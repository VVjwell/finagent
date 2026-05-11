"""进程级单例：MCP 客户端 + 工具 + agent_executor + checkpointer 全局复用。

为什么是单例：
- MultiServerMCPClient 启 5 个 MCP server stdio 子进程，启动一次要秒级。
  绝不能每个请求 / 每个 WS 连接重做一次。
- load_mcp_tools 走每个 server 的 list_tools 也有可观开销，进程内只做一次。
- create_agent 出来的 LangGraph executor 是无状态的，只用 thread_id 区分会话，
  全局共享一个就够。
- InMemorySaver 换成 AsyncSqliteSaver：跨进程重启历史不丢；多会话共用一个库文件。

生命周期靠 FastAPI lifespan 传进来的 AsyncExitStack 兜：进程退出时
MCP 子进程、Sqlite 连接都会被栈倒序关闭。
"""

from __future__ import annotations

import logging
import uuid
from contextlib import AsyncExitStack
from typing import Any

from langchain.agents import create_agent
from langchain_mcp_adapters.client import MultiServerMCPClient

from config import settings
from utils.agent_core import build_llm
from utils.mcp_tools import build_server_configs, gather_tools, log_tool_call

log = logging.getLogger("finagent.session_manager")


def _make_async_sqlite_saver(db_path: str):
    """新老两种 langgraph-checkpoint-sqlite 包路径都试一下，返回 ctxmgr。"""
    try:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver  # type: ignore
    except ImportError:  # 老版本可能挂在不同子模块
        from langgraph.checkpoint.sqlite import AsyncSqliteSaver  # type: ignore
    return AsyncSqliteSaver.from_conn_string(db_path)


class SessionManager:
    def __init__(self) -> None:
        self.client: MultiServerMCPClient | None = None
        self.tools: list = []
        self.checkpointer: Any = None
        self.agent_executor: Any = None
        # 内存里记一下"本进程见过的 thread_id"，REST 列会话用得上；
        # 真正的持久会话历史还是看 SqliteSaver。
        self._known_threads: set[str] = set()

    async def start(self, stack: AsyncExitStack, *, debug: bool = False) -> None:
        settings.llm.require()
        # 跟 cli.py 一样把 agent 要写的目录都预建好
        settings.agent.reports_dir.mkdir(parents=True, exist_ok=True)
        (settings.agent.reports_dir / "daily").mkdir(parents=True, exist_ok=True)
        (settings.agent.reports_dir / "_journal").mkdir(parents=True, exist_ok=True)
        settings.agent.identity_card_file.parent.mkdir(parents=True, exist_ok=True)
        settings.agent.memory_file.parent.mkdir(parents=True, exist_ok=True)
        settings.agent.portfolio_file.parent.mkdir(parents=True, exist_ok=True)

        # log_tool_call 在 server 模式下永远挂上：CLI 用的 print 分支这里不会触发，
        # WS handler 进来时会通过 contextvars 设上事件队列，事件就被路由出去了
        self.client = MultiServerMCPClient(
            build_server_configs(),
            tool_interceptors=[log_tool_call],
        )
        self.tools = await gather_tools(self.client, stack, debug=debug)
        log.info("MCP tools loaded: %d", len(self.tools))

        db_path = settings.agent.workspace_dir / "data" / "checkpoints.sqlite"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.checkpointer = await stack.enter_async_context(
            _make_async_sqlite_saver(str(db_path))
        )
        # AsyncSqliteSaver.setup() 在新版本里自动跑，老版本要显式建表；
        # 没有 setup 方法就跳过
        setup = getattr(self.checkpointer, "setup", None)
        if callable(setup):
            try:
                res = setup()
                if hasattr(res, "__await__"):
                    await res
            except Exception:
                pass

        self.agent_executor = create_agent(
            build_llm(),
            self.tools,
            system_prompt=settings.agent.system_prompt,
            checkpointer=self.checkpointer,
        )
        log.info("agent_executor ready")

    # ------------------------------------------------------------------ #
    #  thread_id 管理
    # ------------------------------------------------------------------ #

    def new_thread_id(self) -> str:
        tid = uuid.uuid4().hex[:8]
        self._known_threads.add(tid)
        return tid

    def register_thread(self, tid: str) -> None:
        self._known_threads.add(tid)

    def list_threads(self) -> list[str]:
        return sorted(self._known_threads)

    def config_for(self, thread_id: str) -> dict:
        return {"configurable": {"thread_id": thread_id}}

    # ------------------------------------------------------------------ #
    #  历史拉取
    # ------------------------------------------------------------------ #

    async def get_history(self, thread_id: str) -> list[dict]:
        """从 checkpointer 取消息历史，归一化成 UI 友好的 dict 列表。"""
        if self.agent_executor is None:
            return []
        try:
            state = await self.agent_executor.aget_state(self.config_for(thread_id))
        except Exception:
            return []
        raw = list((state.values or {}).get("messages") or [])
        out: list[dict] = []
        for m in raw:
            t = getattr(m, "type", "")
            content = getattr(m, "content", "")
            if isinstance(content, list):
                parts: list[str] = []
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "text":
                        parts.append(str(b.get("text", "")))
                    elif isinstance(b, str):
                        parts.append(b)
                content = "\n".join(parts)
            elif not isinstance(content, str):
                content = str(content)
            entry: dict[str, Any] = {"type": t, "content": content}
            tool_calls = []
            for tc in getattr(m, "tool_calls", None) or []:
                if isinstance(tc, dict):
                    tool_calls.append({"name": tc.get("name"), "args": tc.get("args")})
            if tool_calls:
                entry["tool_calls"] = tool_calls
            tcid = getattr(m, "tool_call_id", None)
            if tcid:
                entry["tool_call_id"] = tcid
            name = getattr(m, "name", None)
            if name:
                entry["name"] = name
            out.append(entry)
        return out
