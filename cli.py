"""通用 Agent 客户端。

特点：
- LLM 配置全部来自 .env，通过 config.settings 读
- 通过 MultiServerMCPClient 聚合多个 MCP server：
    * 本地 stdio（如 finMCP.py）
    * 远程 SSE / Streamable HTTP（后续阶段会接入）
  要加新 server，只在 build_mcp_client() 的字典里加一条即可
- 每个 server 使用一条"常驻 session"，整个 agent 运行期间复用同一条 stdio 子进程，
  避免每次工具调用都重启 server
- 支持三种使用方式：
    1. 命令行单次：python agent_client.py "查一下 AAPL 的 PE"
    2. 从文件读： python agent_client.py -f query.txt
    3. 交互模式： python agent_client.py            （回车后逐轮对话，输入 exit 退出）
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from contextlib import AsyncExitStack
from pathlib import Path

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain.agents import create_agent
from langgraph.checkpoint.memory import InMemorySaver

from config import settings
# 引入我们刚才拆分出的 utils
from utils.mcp_tools import build_server_configs, gather_tools, log_tool_call
from utils.agent_core import build_llm, stream_query
from utils.interactive_session import run_interactive_session
from utils.prompt_loader import (
    check_interpreter_is_venv,
    load_briefing_prompt,
    load_kickoff_prompt,
    load_heartbeat_prompt,
    load_reflection_prompt,
    BRIEFING_KINDS,
    HEARTBEAT_INTERVAL_SECONDS,
    HEARTBEAT_MAX_CONSECUTIVE,
    REFLECTION_MIN_USER_TURNS,
)



async def run(
    queries: list[str] | None,
    briefing: str | None = None,
    kickoff: bool = True,
    heartbeat: bool = True,
    reflect: bool = True,
    debug: bool = False,
) -> None:
    # 早失败：配置非法就直接退出，不要等加载完工具才报错
    settings.llm.require()
    check_interpreter_is_venv()

    # 给 filesystem MCP 准备好白名单目录。server 拿到不存在的路径会启动失败。
    settings.agent.reports_dir.mkdir(parents=True, exist_ok=True)
    # 每日仪式的输出专属子目录；确保在 filesystem 白名单内存在可写
    (settings.agent.reports_dir / "daily").mkdir(parents=True, exist_ok=True)
    # 画像目录：存 me.md + identity_card.md（Phase C hot memory）
    settings.agent.identity_card_file.parent.mkdir(parents=True, exist_ok=True)
    # 给 memory MCP 准备好存储文件的父目录。文件本身由 server 首次写入时创建。
    settings.agent.memory_file.parent.mkdir(parents=True, exist_ok=True)
    # Ledger 的 data/ 目录同理，skills/portfolio.py 首次写入时会创建文件。
    settings.agent.portfolio_file.parent.mkdir(parents=True, exist_ok=True)
    # Phase E：会话收尾反思写入 reports/_journal/YYYY-MM-DD.md，预建目录
    (settings.agent.reports_dir / "_journal").mkdir(parents=True, exist_ok=True)

    # briefing模式，比如morning参数，llm读取模板，生成一份报告后退出
    if briefing:
        if briefing not in BRIEFING_KINDS:
            sys.exit(f"--briefing 只支持 {BRIEFING_KINDS}，你给的是 {briefing!r}")
        queries = [load_briefing_prompt(briefing)]
        print(f"🌅 Briefing 模式: {briefing}（单轮执行后退出）")

    # --debug 时挂上，排查 "到底调了哪个工具 / 参数对不对" 用。
    interceptors = [log_tool_call] if debug else []
    client = MultiServerMCPClient(
        build_server_configs(),
        tool_interceptors=interceptors,
    )

    # 默认只打两行真正有用的路径信息（用户可能需要知道写报告和读记忆的地方）。
    # "连接 MCP server(s) / 每个 server 多少工具 / 共发现 N 个" 这类启动噪音只在 --debug 下打。
    if debug:
        print(
            f"🔌 正在连接 MCP server(s): {list(client.connections)} "
            f"| tool_content_format={settings.llm.tool_content_format}"
        )
    print(f"📁 工作目录: {settings.agent.workspace_dir}（敏感文件已屏蔽）")
    print(f"🧠 长期记忆: {settings.agent.memory_file}")
    async with AsyncExitStack() as stack:
        tools = await gather_tools(client, stack, debug=debug)
        if debug:
            print(f"✅ 共发现 {len(tools)} 个工具\n")

        llm = build_llm()
        # 进程结束后历史丢失（跨进程的长期事实靠 memory MCP，不靠这个）。
        #短期记忆（Checkpointer）：使用 LangGraph 的 InMemorySaver 建立检查点系统，并生成一个 thread_id。
        # 这个 thread_id 是串联多轮对话上下文的关键。
        checkpointer = InMemorySaver()
        thread_id = uuid.uuid4().hex[:8]
        session_config: dict = {"configurable": {"thread_id": thread_id}}

        # create_agent
        agent_executor = create_agent(
            llm,
            tools,
            system_prompt=settings.agent.system_prompt,
            checkpointer=checkpointer,
        )
        print(f"💬 会话 thread_id={thread_id}（本次启动内多轮上下文共享；退出即丢）")

        if queries:
            for q in queries:
                await stream_query(agent_executor, q, session_config)
            return
        # 交互模式
        await run_interactive_session(
            agent_executor,
            session_config,
            stream_query=stream_query,
            load_kickoff_prompt=load_kickoff_prompt,
            load_heartbeat_prompt=load_heartbeat_prompt,
            heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
            heartbeat_max_consecutive=HEARTBEAT_MAX_CONSECUTIVE,
            kickoff=kickoff,
            heartbeat=heartbeat,
            reflect=reflect,
            load_reflection_prompt=load_reflection_prompt,
            reflection_min_user_turns=REFLECTION_MIN_USER_TURNS,
        )


# ------------------------------------------------------------------ #
#  CLI
# ------------------------------------------------------------------ #

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="finAgent - LLM + MCP 投研助手")
    p.add_argument("query", nargs="*", help="单轮提问；不传则进入交互模式")
    p.add_argument("-f", "--file", help="从文件读取提问（一行一条）")
    p.add_argument(
        "--briefing",
        choices=BRIEFING_KINDS,
        help="每日仪式模式。加载 prompts/briefing/<kind>.txt 作为单轮指令并退出。",
    )
    p.add_argument(
        "--no-kickoff",
        action="store_true",
        help="关闭交互模式下的启动主动问候（默认启动后 agent 会主动开口）。",
    )
    p.add_argument(
        "--no-heartbeat",
        action="store_true",
        help=(
            "关闭 heartbeat（默认用户连续 "
            f"{HEARTBEAT_INTERVAL_SECONDS // 60} 分钟不响应，"
            "agent 会主动戳一下，最多连续 "
            f"{HEARTBEAT_MAX_CONSECUTIVE} 次就沉默）。"
        ),
    )
    p.add_argument(
        "--no-reflect",
        action="store_true",
        help=(
            "关闭 exit 时的收尾反思（默认 ≥ "
            f"{REFLECTION_MIN_USER_TURNS} 轮实质对话后 agent 会自动"
            "提炼 memory / 写 journal / 维护身份卡，然后才真正退出）。"
        ),
    )
    p.add_argument(
        "--debug",
        action="store_true",
        help="打印每次 MCP 工具调用的 server/name/args（默认关，仅排查用）。",
    )
    return p.parse_args()


def collect_queries(args: argparse.Namespace) -> list[str] | None:
    # briefing 模式走 run() 内部的专用分支，这里不收集 query
    if args.briefing:
        return None
    if args.file:
        path = Path(args.file)
        if not path.exists():
            sys.exit(f"找不到文件: {path}")
        return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if args.query:
        return [" ".join(args.query)]
    return None


def main() -> None:
    """`finagent` console-script 入口；`python cli.py` 也走这里。"""
    args = parse_args()
    queries = collect_queries(args)
    asyncio.run(
        run(
            queries,
            briefing=args.briefing,
            kickoff=not args.no_kickoff,
            heartbeat=not args.no_heartbeat,
            reflect=not args.no_reflect,
            debug=args.debug,
        )
    )


if __name__ == "__main__":
    main()
