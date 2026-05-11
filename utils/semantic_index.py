"""语义记忆索引
为 memory.json 里的 observations 建一个向量索引，用智谱 embedding-3 做
query → top-K 检索，解决"关键词搜不到同义表达"的问题。
- `config.json`      — {"model": ..., "dim": ..., "built_at": ISO, "n_vectors": N}
- `vectors.npy`      — (N, dim) float32，**已 L2 归一化**，点积即 cosine 相似度
- `meta.jsonl`       — 每行一条元信息，顺序与 vectors 行一致：
    {"entity": "...", "obs_idx": 3, "text_hash": "sha256[:16]", "text": "..."}

`text_hash` 用于增量更新：观察项内容变了 hash 就变，需要重新 embed。

在向量检索中，我们不能只存数学向量，因为 AI 搜到一堆数字后并不知道它们代表什么。
所以，你采用的是一种**“三位一体”**的存储方案：
数学数据vectors存余弦相似度
元数据meta存实体名、映射关系
配置信息config存模型、维度、构建时间等信息
程序启动时会先读 config.json，如果发现模型对不上，就会拒绝加载，
强制你执行 rebuild_from_memory()，防止系统出现“牛头不对马嘴”的检索结果。
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from config import settings

# ----------------------------------------------------------------------
#  常量
# ----------------------------------------------------------------------

_INDEX_CONFIG_FILENAME = "config.json"
_INDEX_VECTORS_FILENAME = "vectors.npy"
_INDEX_META_FILENAME = "meta.jsonl"

# 一次 API 调用送几条文本去 embed。智谱 embedding-3 支持 batch，但 64 之内最稳。
_EMBED_BATCH_SIZE = 64

# Embedding 结果 LRU 缓存上限（条）。一条约 1024 维 float = ~4KB，
# 2048 条 ≈ 8MB，重建/增量时重复文本命中率很高，按需再调。
_EMBED_CACHE_MAX_SIZE = 2048

# 没索引过 / 索引缺失时的默认相似度阈值。
DEFAULT_MIN_SCORE = 0.30


# ----------------------------------------------------------------------
#  数据结构
# ----------------------------------------------------------------------


@dataclass
class MetaEntry:
    entity: str
    obs_idx: int
    text_hash: str
    text: str

    def to_json(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "obs_idx": self.obs_idx,
            "text_hash": self.text_hash,
            "text": self.text,
        }

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> "MetaEntry":
        return cls(
            entity=str(obj.get("entity", "")),
            obs_idx=int(obj.get("obs_idx", 0)),
            text_hash=str(obj.get("text_hash", "")),
            text=str(obj.get("text", "")),
        )


@dataclass
class SearchHit:
    entity: str
    obs_idx: int
    text: str
    score: float


# ----------------------------------------------------------------------
#  工具函数
# ----------------------------------------------------------------------


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _l2_normalize(mat: np.ndarray) -> np.ndarray:
    """按行 L2 归一化。零向量保留为零（避免 NaN）。"""
    if mat.size == 0:
        return mat
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    return (mat / norms).astype(np.float32, copy=False)


def _load_memory_entities() -> dict[str, list[str]]:
    """读 memory.json，返回 {entity_name: [obs, ...]}。

    这里刻意不 import memory_guard._load_memory_graph 避免循环依赖。
    """
    path: Path = settings.agent.memory_file
    if not path.exists():
        return {}
    out: dict[str, list[str]] = {}
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "entity":
                    continue
                name = obj.get("name")
                if not isinstance(name, str):
                    continue
                obs = obj.get("observations") or []
                out[name] = [o for o in obs if isinstance(o, str)]
    except OSError:
        return {}
    return out


# ----------------------------------------------------------------------
#  Embedding client
# ----------------------------------------------------------------------

class _BaseEmbeddingClient:
    """所有 embedding provider 的公共基类。

    承担两件与 provider 无关的事：
      · 进程级 LRU 缓存（同一段文本反复 embed 时零成本）
      · 缓存 / API 调用 的锁策略（锁只包缓存读写，调用阶段放锁以保持并发）

    子类只需实现 `_embed_uncached(texts) -> list[list[float]]`——
    拿到一组"确定没命中缓存"的文本，返回**未归一化**的原始向量列表。
    归一化在 `embed()` 组装阶段统一做。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._max_cache_size = _EMBED_CACHE_MAX_SIZE

    @staticmethod
    def _get_cache_key(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _embed_uncached(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    def embed(self, texts: list[str]) -> np.ndarray:
        """给一批文本出 embedding。返回 (N, dim) float32，已 L2 归一化。

        流程：
          1) 持锁：过一遍缓存，命中的写 results，不命中的记下来。
          2) 放锁：对未命中文本批量调 provider（IO / GPU 耗时段不持锁）。
          3) 持锁：新向量写回缓存 + LRU 淘汰。
        """
        if not texts:
            return np.zeros((0, settings.embedding.dim), dtype=np.float32)

        results: list[list[float] | None] = [None] * len(texts)
        missing_indices: list[int] = []
        missing_texts: list[str] = []

        # 阶段 1：命中查询
        with self._lock:
            for i, text in enumerate(texts):
                key = self._get_cache_key(text)
                cached = self._cache.get(key)
                if cached is not None:
                    self._cache.move_to_end(key)  # LRU touch
                    results[i] = cached
                else:
                    missing_indices.append(i)
                    missing_texts.append(text)

        # 阶段 2：provider 调用（不持锁）
        if missing_texts:
            new_vecs = self._embed_uncached(missing_texts)
            if len(new_vecs) != len(missing_texts):
                raise RuntimeError(
                    f"embedding 返回 {len(new_vecs)} 条，预期 {len(missing_texts)} 条"
                )

            # 阶段 3：回填缓存 + LRU 淘汰
            with self._lock:
                for idx, text, vec in zip(missing_indices, missing_texts, new_vecs):
                    key = self._get_cache_key(text)
                    self._cache[key] = vec
                    self._cache.move_to_end(key)
                    results[idx] = vec
                while len(self._cache) > self._max_cache_size:
                    self._cache.popitem(last=False)

        mat = np.asarray(results, dtype=np.float32)
        return _l2_normalize(mat)


class _ZhipuEmbeddingClient(_BaseEmbeddingClient):
    """智谱 OpenAI-compatible 端点（embedding-3 / 2）。网络依赖、按 token 计费。"""

    def __init__(self) -> None:
        super().__init__()
        self._client = None

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if not settings.embedding.api_key:
            raise RuntimeError(
                "ZHIPU_API_KEY 未配置，智谱 embedding 不可用。"
                "请在 .env 里填好 ZHIPU_API_KEY 后重启，或切换 EMBEDDING_PROVIDER=local。"
            )
        # 延迟 import，确保模块顶层纯净
        from openai import OpenAI

        self._client = OpenAI(
            api_key=settings.embedding.api_key,
            base_url=settings.embedding.base_url,
            timeout=settings.embedding.timeout_seconds,
        )
        return self._client

    def _embed_uncached(self, texts: list[str]) -> list[list[float]]:
        client = self._ensure_client()
        out: list[list[float]] = []
        for start in range(0, len(texts), _EMBED_BATCH_SIZE):
            chunk = texts[start : start + _EMBED_BATCH_SIZE]
            kwargs: dict[str, Any] = {
                "model": settings.embedding.model,
                "input": chunk,
            }
            if settings.embedding.dim:
                kwargs["dimensions"] = settings.embedding.dim
            try:
                resp = client.embeddings.create(**kwargs)
            except TypeError:
                # 某些兼容端不认 dimensions 参数，降级重试
                kwargs.pop("dimensions", None)
                resp = client.embeddings.create(**kwargs)
            for d in resp.data:
                out.append(list(d.embedding))
        return out


class _LocalEmbeddingClient(_BaseEmbeddingClient):
    """本地 sentence-transformers 模型。离线、零 token 成本。

    首次 embed 才加载权重：这样进程启动快；HuggingFace 缓存未命中时
    会从网拉一次权重（~100MB～2GB 取决于模型），之后完全离线。
    """

    def __init__(self) -> None:
        super().__init__()
        self._model = None

    def _ensure_model(self):
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise RuntimeError(
                "本地 embedding 需要 sentence-transformers。请先安装：\n"
                "    pip install sentence-transformers\n"
                "或切换回智谱：EMBEDDING_PROVIDER=zhipu"
            ) from e

        self._model = SentenceTransformer(
            settings.embedding.model,
            device=settings.embedding.local_device,  # None 时 ST 自己选
            local_files_only=settings.embedding.local_files_only,
        )
        # 一致性校验：宣称的 dim 必须和模型实际输出对得上，
        # 否则索引里存的向量和查询向量维度对不齐，点积会报错。
        actual_dim = self._model.get_sentence_embedding_dimension()
        if actual_dim != settings.embedding.dim:
            raise RuntimeError(
                f"本地模型 '{settings.embedding.model}' 实际维度 {actual_dim}，"
                f"但 LOCAL_EMBEDDING_DIM 配置为 {settings.embedding.dim}。"
                f"请改 .env 里的 LOCAL_EMBEDDING_DIM={actual_dim} 后重启，"
                f"并用 memory_reindex 重建索引。"
            )
        return self._model

    def _embed_uncached(self, texts: list[str]) -> list[list[float]]:
        model = self._ensure_model()
        # sentence-transformers 内部会按 batch_size 自动分批；我们用同一个常量保持行为一致。
        vecs = model.encode(
            texts,
            batch_size=_EMBED_BATCH_SIZE,
            show_progress_bar=False,
            normalize_embeddings=False,  # 归一化交给基类的 _l2_normalize 统一做
            convert_to_numpy=True,
        )
        return [list(map(float, v)) for v in vecs]


def _build_embedding_client() -> _BaseEmbeddingClient:
    """按 settings.embedding.provider 选实现。"""
    if settings.embedding.provider == "local":
        return _LocalEmbeddingClient()
    return _ZhipuEmbeddingClient()


_embedding_client = _build_embedding_client()


# ----------------------------------------------------------------------
#  Index 本体
# ----------------------------------------------------------------------

# semantic即语义的意思
class SemanticIndex:
    def __init__(self) -> None:
        self._vectors: np.ndarray = np.zeros(
            (0, settings.embedding.dim), dtype=np.float32
        )
        self._meta: list[MetaEntry] = []
        self._built_at: str | None = None
        self._model: str = settings.embedding.model
        self._dim: int = settings.embedding.dim
        self._lock = threading.Lock()

    # ---------- 文件路径 ----------

    @property
    def _dir(self) -> Path:
        return settings.embedding.index_dir

    @property
    def _config_path(self) -> Path:
        return self._dir / _INDEX_CONFIG_FILENAME

    @property
    def _vectors_path(self) -> Path:
        return self._dir / _INDEX_VECTORS_FILENAME

    @property
    def _meta_path(self) -> Path:
        return self._dir / _INDEX_META_FILENAME

    # ---------- 加载 / 保存 ----------

    def load(self) -> bool:
        """从磁盘读索引。成功返回 True，不存在/损坏返回 False 且保持空索引。"""
        with self._lock:
            if not self._config_path.exists():
                return False
            try:
                cfg = json.loads(self._config_path.read_text(encoding="utf-8"))
                model = str(cfg.get("model", ""))
                dim = int(cfg.get("dim", 0))
                built_at = cfg.get("built_at")
            except (OSError, json.JSONDecodeError, ValueError):
                return False

            if dim <= 0:
                return False
            # 模型或维度跟当前配置不一致 → 不加载，等调用方 rebuild
            if model != self._model or dim != self._dim:
                return False

            if not self._vectors_path.exists() or not self._meta_path.exists():
                return False

            try:
                vecs = np.load(self._vectors_path)
                meta: list[MetaEntry] = []
                with self._meta_path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            meta.append(MetaEntry.from_json(json.loads(line)))
                        except (json.JSONDecodeError, ValueError):
                            continue
            except OSError:
                return False

            if vecs.shape[0] != len(meta):
                return False
            if vecs.ndim != 2 or vecs.shape[1] != dim:
                return False

            self._vectors = vecs.astype(np.float32, copy=False)
            self._meta = meta
            self._built_at = built_at if isinstance(built_at, str) else None
            return True

    def save(self) -> None:
        with self._lock:
            self._dir.mkdir(parents=True, exist_ok=True)
            np.save(self._vectors_path, self._vectors)
            with self._meta_path.open("w", encoding="utf-8") as f:
                for m in self._meta:
                    f.write(json.dumps(m.to_json(), ensure_ascii=False) + "\n")
            self._config_path.write_text(
                json.dumps(
                    {
                        "model": self._model,
                        "dim": self._dim,
                        "built_at": self._built_at or _now_iso(),
                        "n_vectors": len(self._meta),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

    # ---------- 状态查询 ----------

    @property
    def size(self) -> int:
        return len(self._meta)

    @property
    def built_at(self) -> str | None:
        return self._built_at

    def stats(self) -> dict[str, Any]:
        per_entity: dict[str, int] = {}
        for m in self._meta:
            per_entity[m.entity] = per_entity.get(m.entity, 0) + 1
        return {
            "model": self._model,
            "dim": self._dim,
            "built_at": self._built_at,
            "n_vectors": len(self._meta),
            "n_entities": len(per_entity),
            "per_entity": per_entity,
        }

    # ---------- 构建 ----------

    def rebuild_from_memory(self) -> dict[str, Any]:
        """全量重建索引：读 memory.json，embed 所有 obs，落盘。"""
        if not settings.embedding.enabled:
            raise RuntimeError("ZHIPU_API_KEY 未配置。")

        entities = _load_memory_entities()
        texts: list[str] = []
        metas: list[MetaEntry] = []
        for entity, obs_list in entities.items():
            for idx, obs in enumerate(obs_list):
                texts.append(obs)
                metas.append(
                    MetaEntry(
                        entity=entity,
                        obs_idx=idx,
                        text_hash=_text_hash(obs),
                        text=obs,
                    )
                )

        if not texts:
            with self._lock:
                self._vectors = np.zeros((0, self._dim), dtype=np.float32)
                self._meta = []
                self._built_at = _now_iso()
            self.save()
            return {
                "n_vectors": 0,
                "n_entities": 0,
                "message": "memory.json 无 observation，已写空索引",
            }

        vecs = _embedding_client.embed(texts)
        if vecs.shape[0] != len(metas):
            raise RuntimeError(
                f"embedding 返回 {vecs.shape[0]} 条，预期 {len(metas)} 条。"
            )

        with self._lock:
            self._vectors = vecs
            self._meta = metas
            self._built_at = _now_iso()
        self.save()
        return self.stats()

    def upsert_entity(self, entity: str, obs_list: list[str]) -> dict[str, Any]:
        """增量：替换 entity 下所有索引项。用于 add_observations 后同步。

        策略很简单——该 entity 先全删、再按新 obs_list 重新插入。
        规模小，操作频率低（人工 trigger），不必 diff。
        """
        if not settings.embedding.enabled:
            return {"skipped": True, "reason": "embedding_disabled"}

        with self._lock:
            keep_mask = np.array(
                [m.entity != entity for m in self._meta], dtype=bool
            )
            if keep_mask.size == 0:
                kept_vecs = np.zeros((0, self._dim), dtype=np.float32)
                kept_meta: list[MetaEntry] = []
            else:
                kept_vecs = self._vectors[keep_mask]
                kept_meta = [m for m, keep in zip(self._meta, keep_mask) if keep]

        if obs_list:
            new_vecs = _embedding_client.embed(obs_list)
            new_meta = [
                MetaEntry(
                    entity=entity,
                    obs_idx=i,
                    text_hash=_text_hash(o),
                    text=o,
                )
                for i, o in enumerate(obs_list)
            ]
        else:
            new_vecs = np.zeros((0, self._dim), dtype=np.float32)
            new_meta = []

        with self._lock:
            if kept_vecs.size == 0 and new_vecs.size == 0:
                self._vectors = np.zeros((0, self._dim), dtype=np.float32)
            else:
                parts = [m for m in (kept_vecs, new_vecs) if m.size > 0]
                self._vectors = np.concatenate(parts, axis=0) if parts else (
                    np.zeros((0, self._dim), dtype=np.float32)
                )
            self._meta = kept_meta + new_meta
            self._built_at = _now_iso()
        self.save()
        return {
            "entity": entity,
            "added": len(new_meta),
            "kept_other": len(kept_meta),
            "n_vectors_total": len(self._meta),
        }

    # ---------- 检索 ----------

    def search(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = DEFAULT_MIN_SCORE,
        entity_filter: Iterable[str] | None = None,
    ) -> list[SearchHit]:
        if not settings.embedding.enabled:
            raise RuntimeError("ZHIPU_API_KEY 未配置。")
        if self.size == 0:
            return []
        q = query.strip()
        if not q:
            return []

        qvec = _embedding_client.embed([q])  # (1, dim), 已归一化
        scores = (self._vectors @ qvec[0]).astype(np.float32)  # cosine via dot

        # entity filter（可选）
        allowed = None
        if entity_filter is not None:
            allowed = set(entity_filter)

        candidates: list[tuple[int, float]] = []
        for i, s in enumerate(scores):
            if allowed is not None and self._meta[i].entity not in allowed:
                continue
            if s < min_score:
                continue
            candidates.append((i, float(s)))

        candidates.sort(key=lambda x: x[1], reverse=True)
        candidates = candidates[:top_k]

        return [
            SearchHit(
                entity=self._meta[i].entity,
                obs_idx=self._meta[i].obs_idx,
                text=self._meta[i].text,
                score=score,
            )
            for i, score in candidates
        ]


# ----------------------------------------------------------------------
#  单例
# ----------------------------------------------------------------------


_INDEX_SINGLETON: SemanticIndex | None = None
_INDEX_SINGLETON_LOCK = threading.Lock()


def get_index() -> SemanticIndex:
    """拿到进程级单例索引。首次调用会尝试 load 磁盘，失败则返回空索引。"""
    global _INDEX_SINGLETON
    if _INDEX_SINGLETON is not None:
        return _INDEX_SINGLETON
    with _INDEX_SINGLETON_LOCK:
        if _INDEX_SINGLETON is None:
            idx = SemanticIndex()
            idx.load()  # 不存在就返回空索引，不抛
            _INDEX_SINGLETON = idx
    return _INDEX_SINGLETON
