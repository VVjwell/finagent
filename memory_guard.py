"""Memory MCP包装
1.对create_entities和add_observations进行包装，实现格式化、去重、和embedding
2.对过多的observation实现压缩
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from config import settings

def wrap_memory_tools(tools: list[BaseTool]) -> list[BaseTool]:
    """memory guard系统入口
    传入memory MCP 工具列表按名字选择性加壳wrapped
    返回新的tool list
    """
    out: list[BaseTool] = []
    for t in tools:
        if t.name == "add_observations":
            out.append(_wrap_add_observations(t))
        elif t.name == "create_entities":
            out.append(_wrap_create_entities(t))
        else:
            out.append(t)
    return out

# ------------------------------------------------------------------ #
#  1.Observation 格式规范
# ------------------------------------------------------------------ #
#: 合法前缀形如 [2026-04-20 | FACT | finnhub] 后接内容。
OBS_PREFIX_RE = re.compile(
    r"^\s*\[\s*(\d{4}-\d{2}-\d{2})\s*\|\s*"
    r"(FACT|VIEW|QUOTE|EVENT|UNKNOWN)\s*\|\s*"
    r"([^\]]+?)\s*\]\s*(.+)$",
    re.DOTALL,
)
VALID_OBS_TYPES = ("FACT", "VIEW", "QUOTE", "EVENT")

# ------------------------------------------------------------------ #
#  2.Observation 压缩阈值
# ------------------------------------------------------------------ #
"""
在知识图谱（Knowledge Graph）或长短期记忆（Memory）系统中
如果一个实体的观测数据（Observations）条数过多
会导致检索时上下文窗口（Context Window）爆炸，不仅浪费 Token，还会分散 AI 的注意力
"""
"""
梯阈值：

40条：开始变臃肿。

60条：显著影响处理效率。

80条：紧急状态，此时该实体的上下文可能已经占据了数千 Token。
"""

OBS_WARN_SOFT = 40
OBS_WARN_HARD = 60
OBS_WARN_URGENT = 80

def _count_warning(total: int, entity_name: str) -> str:
    """根据新的 obs 总数给 agent 的"何时压缩"信号。"""
    if total >= OBS_WARN_URGENT:
        return (
            f" | 🚨 {entity_name} 已 {total} 条 observation，请**现在**征得主人同意后压缩"
        )
    if total >= OBS_WARN_HARD:
        return (
            f" | ⚠ {entity_name} 已 {total} 条，强烈建议找个自然时机跟主人商量压缩"
        )
    if total >= OBS_WARN_SOFT:
        return f" | {entity_name} 当前 {total} 条，到该考虑压缩的量级了"
    return ""

# ------------------------------------------------------------------ #
#  3.工具函数：归一化、哈希值计算、前缀修复、RAG语义检索embedding
# ------------------------------------------------------------------ #

def _normalize_for_dedup(text: str) -> str:
    """去重：把 observation 标准化后取短哈希，用于判断"语义/字面几乎一致"。
    """
    # 剥离前缀[2026-04-20 | FACT | source]
    m = OBS_PREFIX_RE.match(text)
    body = m.group(4) if m else text
    #全部小写
    body = body.lower()
    # 删掉所有标点符号只保留字母、数字、中日韩文字
    body = re.sub(r"[^\w\u4e00-\u9fff]+", " ", body, flags=re.UNICODE)
    body = re.sub(r"\s+", " ", body).strip()
    # 短哈希生成： 对处理后的字符串取 SHA-256，并截取前 16 位
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _ensure_prefix(obs: str) -> tuple[str, bool]:
    """确保每一条存入 memory.json 的记忆都拥有结构化的元数据
    """
    # 正则检测前缀
    if OBS_PREFIX_RE.match(obs):
        return obs, False
    today = date.today().isoformat()
    stripped = obs.lstrip()
    # 返回一个元组：(修正后的字符串, 是否被修改过)
    return f"[{today} | UNKNOWN | auto] {stripped}", True


def _load_memory_graph() -> dict[str, dict]:
    """读 memory.json,转换为一个HashMap。{entity_name: entity_dict(包含name、observations、relations等)}
    """
    path: Path = settings.agent.memory_file
    if not path.exists():
        return {}

    entities: dict[str, dict] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue  # 单行坏了不要把整个图废掉
                if obj.get("type") == "entity" and isinstance(obj.get("name"), str):
                    entities[obj["name"]] = obj
    except OSError:
        return {}
    return entities


def _flatten_result(result: Any) -> str:
    """把 MCP 工具返回的 content blocks 列表压成字符串。

    跟 agent_client._flatten_block_content 职责一样，这里复写一份避免循环 import。
    """
    if isinstance(result, tuple) and len(result) == 2:
        result = result[0]
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        parts: list[str] = []
        for item in result:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p)
    return str(result)


def _schedule_index_sync(entity_names: set[str]) -> None:
    """实现RAG中语义搜索功能。接收entity_names集合,实现embedding
    """
    if not entity_names or not settings.embedding.enabled:
        return
    try:
        from utils.semantic_index import _load_memory_entities, get_index
        
        # 加载memory_entities
        snapshot = _load_memory_entities() 
        idx = get_index()
        # 协程，负责同步工作
        async def _runner() -> None:
            # 定义一个loop
            loop = asyncio.get_running_loop()
            for name in entity_names:
                obs_list = snapshot.get(name, [])
                try:
                    # idx.upsert_entity调用embedding对向量数据库写入，阻塞性IO
                    # 异步环境使用run_in_executor将阻塞性操作转换为异步
                    await loop.run_in_executor(
                        None, idx.upsert_entity, name, obs_list
                    )
                except Exception: 
                    pass

        loop = asyncio.get_running_loop()
        loop.create_task(_runner())
    except RuntimeError:
        pass
    except Exception: 
        pass

# ------------------------------------------------------------------ #
#  4.对add_observations和create_entity的Wrapper 实现
# ------------------------------------------------------------------ #

#此函数没办法让AI修改错误的记忆，只能去重和格式化
def _wrap_add_observations(tool: BaseTool) -> BaseTool:
    """拦截并增强add_observations：格式化 + 去重 + 附带状态提示。
       add->去重+增强->写入memory.json
    """
    if not isinstance(tool, StructuredTool) or tool.coroutine is None:
        return tool

    original = tool.coroutine

    def _format_result_text(text: str) -> Any:
        if tool.response_format == "content_and_artifact":
            return text, None
        return text

    async def wrapped(**kwargs: Any) -> Any:
        try:
            # 尝试从输入中获取 observations 列表
            # observations: list[{entityName, contents: list[str]}]
            payloads = kwargs.get("observations") or []
            if not isinstance(payloads, list):
                return await original(**kwargs)  # 参数形状不对，交给原工具报错
            
            # 加载当前的知识图谱
            graph = _load_memory_graph()
            report_lines: list[str] = []
            forwarded_payloads: list[dict] = []

            for payload in payloads:
                if not isinstance(payload, dict):
                    continue
                entity_name = payload.get("entityName") or payload.get("entity_name") or ""
                contents = payload.get("contents") or []
                if not isinstance(contents, list):
                    continue
                # 拿到现有的observation
                existing_obs = (
                    graph.get(entity_name, {}).get("observations", []) or []
                )
                # 归一化并拿到哈希集合用于后续去重
                existing_hashes = {_normalize_for_dedup(o) for o in existing_obs}

                kept: list[str] = []
                auto_fixed = 0
                deduped = 0
                
                # 数据清洗与前缀修复
                for c in contents:
                    if not isinstance(c, str):
                        continue
                    fixed, was_autofixed = _ensure_prefix(c)
                    h = _normalize_for_dedup(fixed)
                    if h in existing_hashes:
                        deduped += 1
                        continue
                    existing_hashes.add(h)
                    if was_autofixed:
                        auto_fixed += 1
                    kept.append(fixed)

                # 状态反馈：为每个实体生成一行进度报告，告诉AI哪些被新增了，哪些是被过滤掉的重复项。
                total_after = len(existing_obs) + len(kept)
                line = (
                    f"· {entity_name}: 新增 {len(kept)} / 去重 {deduped}"
                    + (f" / 自动补前缀 {auto_fixed}" if auto_fixed else "")
                    + _count_warning(total_after, entity_name)
                )
                report_lines.append(line)

                if kept:
                    forwarded_payloads.append(
                        {"entityName": entity_name, "contents": kept}
                    )

            header = "\n".join(report_lines) if report_lines else ""
            if auto_fixed_any := any("自动补前缀" in ln for ln in report_lines):
                header += (
                    "\n\n（提示：下次写 observation 请自带 "
                    "`[YYYY-MM-DD | FACT|VIEW|QUOTE|EVENT | 来源]` 前缀）"
                )

            if not forwarded_payloads:
                return _format_result_text(
                    (header + "\n\n（全部是重复或无效内容，未写入 memory）").strip()
                )

            raw_result = await original(observations=forwarded_payloads)
            inner_text = _flatten_result(raw_result)

            # 语义索引增量同步（后台任务，不阻塞返回）
            touched = {p["entityName"] for p in forwarded_payloads if p.get("entityName")}
            _schedule_index_sync(touched)

            return _format_result_text((header + "\n---\n" + inner_text).strip())

        except Exception as e: 
            # 出问题就降级为"按原样调原工具"
            fallback = await original(**kwargs)
            return _format_result_text(
                _flatten_result(fallback) + f"\n\n(⚠ memory_guard fallback: {e})"
            )

    return StructuredTool(
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
        coroutine=wrapped,
        response_format=tool.response_format,
        metadata=tool.metadata,
    )

# 新的entities就是新的股票/商品啦
def _wrap_create_entities(tool: BaseTool) -> BaseTool:
    """
    拦截并增强“创建实体”（create_entities）这一工具。它的逻辑与之前的 _wrap_add_observations 类似
    但侧重点在于新实体的初始化校验和语义索引的首次同步。"""
    if not isinstance(tool, StructuredTool) or tool.coroutine is None:
        return tool

    original = tool.coroutine

    def _format_result_text(text: str) -> Any:
        if tool.response_format == "content_and_artifact":
            return text, None
        return text

    async def wrapped(**kwargs: Any) -> Any:
        try:
            entities = kwargs.get("entities") or []
            if not isinstance(entities, list):
                return await original(**kwargs)

            auto_fixed_total = 0
            fixed_entities = []
            # 对每个ent检查observation的格式并补全
            for ent in entities:
                if not isinstance(ent, dict):
                    fixed_entities.append(ent)
                    continue
                obs_list = ent.get("observations") or []
                fixed_obs = []
                for o in obs_list:
                    if isinstance(o, str):
                        f, was = _ensure_prefix(o)
                        if was:
                            auto_fixed_total += 1
                        fixed_obs.append(f)
                    else:
                        fixed_obs.append(o)
                ent = {**ent, "observations": fixed_obs}
                fixed_entities.append(ent)

            raw_result = await original(entities=fixed_entities)
            text = _flatten_result(raw_result)
            # 教育机制：通过在返回结果中追加提示，动态引导 AI（LLM）改进后续生成的指令质量。
            if auto_fixed_total:
                text += (
                    f"\n\n（提示：{auto_fixed_total} 条初始 observation 被自动补前缀，"
                    f"下次创建时请自带 `[YYYY-MM-DD | TYPE | source]`）"
                )

            # 新 entity 带初始 obs 的，一并索引
            touched = {
                e.get("name") for e in fixed_entities
                if isinstance(e, dict) and isinstance(e.get("name"), str)
                and (e.get("observations") or [])
            }
            # 调用我们之前分析过的 _schedule_index_sync，确保新知识能被向量检索搜到。
            _schedule_index_sync({n for n in touched if n})

            return _format_result_text(text)
        except Exception as e:  # noqa: BLE001
            fallback = await original(**kwargs)
            return _format_result_text(
                _flatten_result(fallback) + f"\n\n(⚠ memory_guard fallback: {e})"
            )

    return StructuredTool(
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
        coroutine=wrapped,
        response_format=tool.response_format,
        metadata=tool.metadata,
    )


