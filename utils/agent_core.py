from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import RemoveMessage
from langchain_openai import ChatOpenAI

from config import settings

# ------------------------------------------------------------------ #
#  LLM
# ------------------------------------------------------------------ #
_AGENT_MODEL_NODES = {"agent", "model"}


def _tool_call_ids(message: Any) -> set[str]:
    """提取 AIMessage 上声明的 tool_call id。"""
    ids: set[str] = set()
    for call in getattr(message, "tool_calls", None) or []:
        if isinstance(call, dict) and call.get("id"):
            ids.add(str(call["id"]))
    for call in (getattr(message, "additional_kwargs", None) or {}).get("tool_calls", []) or []:
        if isinstance(call, dict) and call.get("id"):
            ids.add(str(call["id"]))
    return ids


def _orphan_tool_removals(messages: list[Any]) -> list[RemoveMessage]:
    """找出没有前置 tool_call 的 ToolMessage。

    OpenAI-compatible 接口要求 role=tool 必须紧跟某条 assistant tool_call。
    历史裁剪如果切掉了 assistant、留下 tool，会在下一轮请求时报 400。
    """
    pending_tool_ids: set[str] = set()
    removals: list[RemoveMessage] = []

    for msg in messages:
        msg_type = getattr(msg, "type", "")
        if msg_type == "ai":
            pending_tool_ids.update(_tool_call_ids(msg))
            continue
        if msg_type != "tool":
            continue

        tool_call_id = getattr(msg, "tool_call_id", None)
        if tool_call_id and str(tool_call_id) in pending_tool_ids:
            pending_tool_ids.remove(str(tool_call_id))
            continue

        msg_id = getattr(msg, "id", None)
        if msg_id:
            removals.append(RemoveMessage(id=msg_id))
    return removals


async def _repair_tool_history(agent_executor: Any, config: dict | None) -> None:
    """在模型调用前清理已存在的孤儿 ToolMessage。"""
    if not config:
        return
    try:
        state = await agent_executor.aget_state(config)
        messages = list((state.values or {}).get("messages") or [])
        removals = _orphan_tool_removals(messages)
        if removals:
            await agent_executor.aupdate_state(config, {"messages": removals})
    except Exception:
        return


async def _prune_short_term_history(agent_executor: Any, config: dict | None) -> None:
    """裁剪 LangGraph checkpointer 里的短期消息历史。

    持久事实不靠这里保存，而是靠 memory / portfolio ledger。这里保留最近几轮对话，
    主要是降低后续轮次的 prompt 体积和首 token 等待时间。
    """
    max_messages = settings.agent.max_history_messages
    if not config or max_messages <= 0:
        return
    try:
        state = await agent_executor.aget_state(config)
        messages = list((state.values or {}).get("messages") or [])
        excess = len(messages) - max_messages
        if excess <= 0:
            return
        cutoff = excess
        while cutoff < len(messages) and getattr(messages[cutoff], "type", "") == "tool":
            cutoff += 1
        removals = [
            RemoveMessage(id=msg.id)
            for msg in messages[:cutoff]
            if getattr(msg, "id", None)
        ]
        if removals:
            await agent_executor.aupdate_state(config, {"messages": removals})
            await _repair_tool_history(agent_executor, config)
    except Exception:
        # 历史裁剪是性能优化，不应该影响主对话。
        return

def build_llm() -> ChatOpenAI:
    settings.llm.require()
    kwargs: dict[str, Any] = dict(
        model=settings.llm.model,
        api_key=settings.llm.api_key,
        temperature=settings.llm.temperature,
    )
    if settings.llm.base_url:
        kwargs["base_url"] = settings.llm.base_url
    return ChatOpenAI(**kwargs)


def _extract_stream_text(content: Any) -> str:
    """从一个 AIMessageChunk.content 里抠出纯文本片段。

    DeepSeek 等 OpenAI 兼容端返回的是 str，但 Anthropic / OpenAI 原生多模态走 blocks 列表。
    这里把两种情况统一掉。非 text block（图像/工具调用元数据等）流式阶段直接忽略。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return ""


async def stream_query_events(
    agent_executor,
    query: str,
    config: dict | None = None,
    *,
    show_user: bool = True,
) -> AsyncIterator[dict]:
    """单轮对话的结构化事件流。

    把"产生事件"和"打印事件"彻底拆开：本函数只 yield 事件，不碰任何 I/O。
    CLI 端由 stream_query() 把事件翻译成 print；后续的 WebSocket 端可以直接把
    事件 JSON 化推给前端。

    yield 出的事件类型：
        {"type": "user", "text": ...}        # 仅 show_user=True；用户消息回显
        {"type": "assistant_start"}           # 助手开口前的一次性信号
        {"type": "token", "text": ...}        # LLM 文本片段（流式）
        {"type": "turn_break"}                # 工具调用后助手再次开口，段落分隔
        {"type": "done", "had_content": bool} # 整轮结束，had_content 给 UI 决定是否补换行

    `stream_mode="messages"` 和默认的 "updates" 的区别：
      updates  → 每个 graph 节点跑完一次给一个 chunk（一整段 AIMessage 一次性给你)
      messages → 每来一个 LLM token 立刻给你一个 AIMessageChunk
    前者用户要等一整段生成完才看到；后者像 ChatGPT 那样字一个个冒出来，体感快很多。

    show_user: False 用于启动问候 / heartbeat 等"幕后注入的 user turn"，
    用户只该看到 agent 的主动开口，不该看到触发它的内部提示文本。
    """
    if show_user:
        yield {"type": "user", "text": query}
    yield {"type": "assistant_start"}

    await _repair_tool_history(agent_executor, config)

    current_run_id: Any = None
    had_content = False
    async for message_chunk, metadata in agent_executor.astream(
        {"messages": [("user", query)]},
        config=config,
        stream_mode="messages",
    ):
        # messages 流模式会同时流出 AIMessageChunk（LLM 文本）和 ToolMessage（工具返回）。
        # 工具返回由 log_tool_call 拦截器另行处理，不要在这里重复；按节点名过滤即可。
        if metadata.get("langgraph_node") not in _AGENT_MODEL_NODES:
            continue

        content = getattr(message_chunk, "content", None)
        if not content:
            # 纯 tool_call 的 chunk（content 为空、只带 tool_call_chunks）跳过
            continue

        # 同一条 AIMessage 的所有 chunk 共享 run_id；切到新 run_id 说明是
        # "工具调用 → 工具返回 → 模型再次开口" 的第二段输出，发一个 turn_break
        # 让 UI 把两段文本区隔开。
        run_id = metadata.get("run_id")
        if had_content and run_id != current_run_id:
            yield {"type": "turn_break"}
        current_run_id = run_id

        text = _extract_stream_text(content)
        if text:
            yield {"type": "token", "text": text}
            had_content = True

    yield {"type": "done", "had_content": had_content}

    await _prune_short_term_history(agent_executor, config)


async def stream_query(
    agent_executor,
    query: str,
    config: dict | None = None,
    *,
    show_user: bool = True,
) -> None:
    """CLI 入口：消费 stream_query_events 把事件打印到 stdout。

    保留对外签名和打印字节序列与历史版本完全一致，旧的 CLI 调用方零感知。
    新增的 WebSocket / SSE 端不应该再调这个函数，直接消费 stream_query_events。
    """
    async for ev in stream_query_events(agent_executor, query, config, show_user=show_user):
        kind = ev["type"]
        if kind == "user":
            print(f"\n👤 {ev['text']}")
        elif kind == "assistant_start":
            # show_user=False 时（kickoff/heartbeat 等幕后注入）前面没有 user 行，
            # 补一个空行避免 "🤖 " 紧贴上方的加载日志。
            if not show_user:
                print()
            print("🤖 ", end="", flush=True)
        elif kind == "token":
            print(ev["text"], end="", flush=True)
        elif kind == "turn_break":
            print()
        elif kind == "done":
            if ev["had_content"]:
                # 结尾换行，避免下一轮 "> " 提示符挤在同一行
                print()