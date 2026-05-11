"""语义记忆检索
@mcp.tool(): 将此函数暴露给 AI Agent。
AI自己决定是否调用
RAG系统里数据通常存在:
1.存储层：比如 memory.json，存的是具体的文字和逻辑关系（谁是谁）。
2.检索层（idx）：存的是文字对应的“数学特征”

"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from typing import Any

from skills import mcp
from utils.semantic_index import DEFAULT_MIN_SCORE, SearchHit, get_index
from config import settings


# ------------------------------------------------------------------ #
#  格式化
# ------------------------------------------------------------------ #

def _format_hits(hits: list[SearchHit], query: str) -> str:
    """
    将向量数据库检索出来的原始结果（Hits）转换成 AI（Agent）能听懂、能看懂的文字报告
    """
    if not hits:
        return (
            f"对 query {query!r} 无相关记忆（score < 阈值）。\n"
            f"如果你认为应该有，试试：\n"
            f"  · 降低 min_score（当前默认 {DEFAULT_MIN_SCORE}）\n"
            f"  · 用 memory.search_nodes 做关键词兜底\n"
            f"  · 用 memory.open_nodes(names=[...]) 列出具体 entity"
        )
    lines: list[str] = [f"语义检索 query={query!r} 命中 {len(hits)} 条："]
    for i, h in enumerate(hits, 1):
        lines.append(
            f"{i}. [{h.entity} / obs#{h.obs_idx}]  score={h.score:.3f}\n"
            f"     {h.text}"
        )
    lines.append(
        "\n想深挖某 entity 的其他 observation：memory.open_nodes(names=[\"该 entity\"])"
    )
    return "\n".join(lines)

def _disabled_message() -> str:
    return (
        "⚠ 语义检索不可用：LLM_API_KEY 未配置。\n"
        "请在 .env 里设 `LLM_API_KEY=...`（智谱开放平台的 key），重启 agent 后生效。\n"
        "暂时可以退回 memory.search_nodes（关键词匹配）。"
    )

def _empty_index_message() -> str:
    return (
        "⚠ 语义索引为空。请先用 memory_reindex() 做一次全量索引，"
        "或者确认 memory.json 里有 observation。"
    )


def _keyword_fallback(query: str, top_k: int, entity_filter: list[str] | None = None) -> str:
    terms = [
        t.lower()
        for t in re.findall(r"[A-Za-z0-9.]+|[\u4e00-\u9fff]{1,}", query)
        if len(t.strip()) >= 2 or any(ch.isdigit() for ch in t)
    ]
    if not terms:
        return ""
    allowed = set(entity_filter or [])
    hits: list[tuple[int, str, int, str]] = []
    try:
        with settings.agent.memory_file.open("r", encoding="utf-8") as f:
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
                entity = obj.get("name")
                if not isinstance(entity, str):
                    continue
                if allowed and entity not in allowed:
                    continue
                observations = obj.get("observations") or []
                for idx, obs in enumerate(observations):
                    if not isinstance(obs, str):
                        continue
                    haystack = f"{entity} {obs}".lower()
                    score = sum(1 for term in terms if term in haystack)
                    if score:
                        hits.append((score, entity, idx, obs))
    except OSError:
        return ""
    if not hits:
        return ""
    hits.sort(key=lambda item: item[0], reverse=True)
    lines = ["关键词兜底命中："]
    for i, (_, entity, idx, obs) in enumerate(hits[: max(1, min(top_k, 10))], 1):
        lines.append(f"{i}. [{entity} / obs#{idx}] {obs}")
    return "\n".join(lines)


def _timeout_message(query: str, top_k: int, entity_filter: list[str] | None = None) -> str:
    fallback = _keyword_fallback(query, top_k, entity_filter)
    message = (
        f"⚠ 语义检索超过 {settings.embedding.timeout_seconds:.0f}s，已中止等待。\n"
        "通常是本地 embedding 模型首次加载/下载太慢。请先用 memory.search_nodes "
        "做关键词兜底，或在终端预热/下载本地模型后重启 agent。"
    )
    if fallback:
        message += "\n\n" + fallback
    return message

# ------------------------------------------------------------------ #
#  MCP tools
# ------------------------------------------------------------------ #

@mcp.tool()
def memory_semantic_search(
    query: str,
    top_k: int = 5,
    min_score: float | None = None,
    entity_filter: list[str] | None = None,
) -> str:
    """按**语义相似度**查找 memory.json 里的 observation，返回 top-K 匹配。
      · semantic_search 走 embedding 向量空间，语义接近的观点/事实都能召回
    什么时候用：
      · 用户问"我对 X 的看法"、"我担心过什么"、"有没有提过 Y"——优先用这个
      · 想找相关上下文但不确定关键词——优先用这个
      · 明确知道 entity 名（如 "Microsoft"）想看全量 obs —— 用 memory.open_nodes 更直接

    参数:
        query         : 自然语言查询。越具体效果越好。
        top_k         : 返回前 K 条，默认 5
        min_score     : 相似度阈值 [0, 1]。省略则用默认 0.3。
                         结果稀疏时可以降到 0.2；结果太多时升到 0.4+
        entity_filter : 若非空，只在这些 entity 下搜。比如 ["Microsoft", "Intel"]

    返回：带坐标（entity / obs_idx / score）的命中列表。
    看到相关结果后，如需补充上下文可以 memory.open_nodes(names=[...]) 深挖。

    注意：
      · 精确事实（ticker、公司名、持仓股数、成交记录）优先用 memory.search_nodes
        或 portfolio_*，不要先跑语义检索。
      · 如果刚写完报告或已经能回答用户，不要为了顺手维护记忆调用本工具；
        这类低优先级沉淀交给 exit 反思。
    """
    
    if not settings.embedding.enabled:
        return _disabled_message()
    # 拿到检索层
    idx = get_index()
    if idx.size == 0:
        return _empty_index_message()
    bounded_top_k = max(1, min(top_k, 20))
    effective_min_score = min_score if min_score is not None else DEFAULT_MIN_SCORE
    effective_filter = entity_filter if entity_filter else None
    try:
        # 调用向量数据库的search方法，传入query、top_k、min_score、entity_filter
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(
            idx.search,
            query=query,
            top_k=bounded_top_k,
            min_score=effective_min_score,
            entity_filter=effective_filter,
        )
        try:
            hits = future.result(timeout=settings.embedding.timeout_seconds)
        except TimeoutError:
            future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            return _timeout_message(query, bounded_top_k, effective_filter)
        executor.shutdown(wait=False)
    except Exception as e:  # noqa: BLE001
        return f"⚠ 语义检索失败：{type(e).__name__}: {e}"
    # 返回查询结果并格式化
    return _format_hits(hits, query)


@mcp.tool()
def memory_reindex() -> str:
    """全量重建语义索引（读 memory.json → 调智谱 embedding-3 → 存 data/memory_index/）。
    什么时候调：
      · 首次启用（索引还没建）
      · 批量手动改过 memory.json（绕过 MCP 的情况）
      · 非必要不调用——memory.add_observations 的功能会自动增量更新
    一次重建对当前规模（几十到几百条 obs）不到 1 分钱、2 秒内完成。
    高开销:针对幻觉和数据一致性
        ·如果 AI 发现自己搜到的信息和之前读到的文件对不上，调用 memory_reindex 就能完成强制同步。
    """
    if not settings.embedding.enabled:
        return _disabled_message()
    # 老检索层
    idx = get_index()
    try:
        # 调用向量数据库的rebuild_from_memory方法，重建索引
        stats: dict[str, Any] = idx.rebuild_from_memory()
    except Exception as e:  # noqa: BLE001
        return f"⚠ 重建失败：{type(e).__name__}: {e}"
    per = stats.get("per_entity", {})
    lines = [
        "✅ 语义索引已重建",
        f"  · 模型:        {stats.get('model')}",
        f"  · 维度:        {stats.get('dim')}",
        f"  · 向量数:      {stats.get('n_vectors')}",
        f"  · 实体数:      {stats.get('n_entities')}",
        f"  · 构建时间:    {stats.get('built_at')}",
    ]
    if per:
        lines.append("  · 各 entity 观察数:")
        for name, n in sorted(per.items(), key=lambda x: -x[1]):
            lines.append(f"      - {name}: {n}")
    return "\n".join(lines)
