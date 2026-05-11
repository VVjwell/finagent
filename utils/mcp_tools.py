from __future__ import annotations

import asyncio
import contextvars
import fnmatch
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Awaitable, Callable

from langchain_core.tools import BaseTool, StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.interceptors import (
    MCPToolCallRequest,
    MCPToolCallResult,
)
from langchain_mcp_adapters.sessions import (
    SSEConnection,
    StdioConnection,
    StreamableHttpConnection,
    WebsocketConnection,
)
from langchain_mcp_adapters.tools import load_mcp_tools

from config import settings
from memory_guard import wrap_memory_tools

PROJECT_ROOT = Path(__file__).resolve().parent.parent

MCPConnection = (
    StdioConnection | SSEConnection | StreamableHttpConnection | WebsocketConnection
)

# ------------------------------------------------------------------ #
#  MCP server 配置
# ------------------------------------------------------------------ #

def build_server_configs() -> dict[str, MCPConnection]:
    """集中管理所有 MCP server 连接配置。

    加新 server：往这个字典里加一条即可。
    可选 server（依赖某个 API key）通过条件判断接入，
    没配 key 就静默跳过，不影响其它 server。
    """
    servers: dict[str, MCPConnection] = {
        "finMCP": {
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(PROJECT_ROOT / "finMCP.py")],
        },
        "time": {
            "transport": "stdio",
            "command": sys.executable,
            "args": _time_server_args(),
        },
        "filesystem": {
            "transport": "stdio",
            "command": _npx_command(),
            "args": [
                "-y",
                "@modelcontextprotocol/server-filesystem",
                str(settings.agent.workspace_dir),
            ],
        },
        # Memory MCP（官方）：基于知识图谱的长期记忆，跨会话持久。
        # 注入环境变量给MEMORY_FILE_PATH
        "memory": {
            "transport": "stdio",
            "command": _npx_command(),
            "args": ["-y", "@modelcontextprotocol/server-memory"],
            "env": {"MEMORY_FILE_PATH": str(settings.agent.memory_file)},
        },
    }

    # 直接 Tavily / Firecrawl MCP 不再暴露给模型，避免和 deep_search 抢职责。
    # deep_search 会在代码内部按需使用 TAVILY_API_KEY，保持单一搜索入口。

    # FRED MCP（本地 stdio，Python 实现）：访问美联储 80 万+ 条经济时间序列
    # （CPI / 失业率 / PMI / 利率曲线 / GDP ...）。补齐宏观面，是个股研究之外的另一条腿。
    # 33 个工具：series / categories / releases / sources / tags / geo（regional maps）。
    # 免费，FRED key 在 https://fredaccount.stlouisfed.org/apikeys 申请。
    #
    # 选 PyPI 包 floriancaro/fred-mcp-server 而非同名 npm 包：
    # 同名的 npm 版（stefanoamorelli）在 Windows 上有个 main 检测的 bug
    # （`import.meta.url === 'file://' + process.argv[1]` 在 Windows 永远不成立）
    # 启动后立即静默退出。Python 版没坑，且工具数量更多、license 更友好（MIT）。
    if settings.fred_api_key:
        servers["fred"] = {
            "transport": "stdio",
            "command": _venv_script("fred-mcp-server"),
            "args": [],
            "env": {"FRED_API_KEY": settings.fred_api_key},
        }

    return servers


def _npx_command() -> str:
    """跨平台 npx 命令名。Windows 下实际是 npx.cmd，PATH 解析会失败需要显式带后缀。"""
    return "npx.cmd" if sys.platform == "win32" else "npx"


def _venv_script(name: str) -> str:
    """定位当前 venv 的 Scripts/bin 里的某个 console script，跨平台。

    Windows 上同名命令实际文件是 name.exe，subprocess 直接走 CreateProcess 不会
    像 cmd 那样自动补 PATHEXT，所以这里要显式找出真实路径。
    """
    scripts_dir = Path(sys.executable).parent
    for candidate in (scripts_dir / f"{name}.exe", scripts_dir / name):
        if candidate.exists():
            return str(candidate)
    return name  # 兜底：交给 PATH 解析


def _time_server_args() -> list[str]:
    args = ["-m", "mcp_server_time"]
    if settings.agent.local_timezone:
        args += ["--local-timezone", settings.agent.local_timezone]
    return args


# ------------------------------------------------------------------ #
#  工具白名单：每个 server 实际暴露给 LLM 的 tool 子集
# ------------------------------------------------------------------ #
#
TOOL_ALLOWLIST: dict[str, set[str] | None] = {
    # finMCP里的工具全能调用，就是我写的skills里的
    "finMCP": None,
    # 外部工具
    "time": None,
    "filesystem": {
        "read_file",
        "write_file",
        "edit_file",
        "list_directory",
        "create_directory",
    },
    # read_graph 会把整张图一次性 dump 回来，太贵，用 search_nodes + open_nodes 代替即可。
    "memory": {
        "create_entities",
        "create_relations",
        "add_observations",
        "search_nodes",
        "open_nodes",
        "delete_entities",      # 删除整个实体
        "delete_observations",  # 删除特定的观测记录
        "delete_relations",     # 删除实体间的关系
    },
    # FRED 33 个都是独立宏观序列，很难提前挑；要省整个 server 的 schema 开销，
    "fred": None,
}


async def _gather_tools(
    client: MultiServerMCPClient,
    stack: AsyncExitStack,
    *,
    debug: bool = False,
) -> list[BaseTool]:
    """为每个 server 开一条常驻 session，收齐所有工具。

    Session 的生命周期绑到外部传入的 AsyncExitStack，调用方负责 `async with` 管理。
    debug=False 时完全静默加载；仅"白名单全滤空"这类真异常才会 print（那是配错了需要提醒）。
    """
    flatten = settings.llm.tool_content_format == "string"
    all_tools: list[BaseTool] = []
    for name in client.connections:
        session = await stack.enter_async_context(client.session(name))
        # 注意：client.tool_interceptors 必须显式传给 load_mcp_tools——
        # 这个参数只有走 client.get_tools() 时才会自动传递。走
        # client.session(...) + load_mcp_tools 这条路径需要手动带上，
        # 否则 log_tool_call 这类拦截器永远不会被触发。
        tools = await load_mcp_tools(
            session,
            server_name=name,
            tool_interceptors=client.tool_interceptors,
        )

        # 白名单过滤：未登记的 server 等价于 None（全保留）
        raw_count = len(tools)
        allowlist = TOOL_ALLOWLIST.get(name)
        if allowlist is not None:
            tools = [t for t in tools if t.name in allowlist]
            if not tools:
                # 这是配置错误（通常是工具名拼写/大小写对不上），必须提醒——无条件打
                print(
                    f"   ⚠ {name}: 白名单把工具全滤空了，回退到保留全部（请检查 TOOL_ALLOWLIST）"
                )
                tools = await load_mcp_tools(
                    session,
                    server_name=name,
                    tool_interceptors=client.tool_interceptors,
                )

        # filesystem 工具叠一层路径守卫，拦截 .env / memory.json / .venv 等敏感路径。
        # 必须在 string-content wrapper 之前做——那个 wrapper 覆盖了 coroutine，
        # 先叠守卫保证守卫逻辑跑在最外层。
        if name == "filesystem":
            tools = [_wrap_filesystem_tool_with_guard(t) for t in tools]
        # 对memory server进行白名单过滤
        # add observations和create entities两个功能
        if name == "memory":
            tools = wrap_memory_tools(tools)
        if flatten:
            tools = [_wrap_tool_for_string_content(t) for t in tools]
        all_tools.extend(tools)
        if debug:
            if len(tools) == raw_count:
                print(f"   · {name}: {len(tools)} 个工具")
            else:
                print(
                    f"   · {name}: {len(tools)} 个工具（白名单过滤 {raw_count} → {len(tools)}）"
                )
    return all_tools


# 对外导出的稳定名字（给 cli.py 用）
gather_tools = _gather_tools

# ------------------------------------------------------------------ #
#  Tool 调用拦截 / 事件路由
# ------------------------------------------------------------------ #
#
# log_tool_call 同时是 CLI 的 debug print 入口，也是 WebSocket 端把工具调用
# 推给前端的事件源。靠 contextvars.ContextVar 做按 asyncio task 隔离的路由：
#   · CLI 路径：current_event_queue == None → 走 print（旧行为，仅 --debug 时挂载）
#   · WS 路径：handler 进来时 set 一个 asyncio.Queue 进 ContextVar →
#             所有 LangChain 触发的工具调用都在同一 task 上下文里，事件
#             自动入队，不污染 server stdout

current_event_queue: contextvars.ContextVar[asyncio.Queue | None] = (
    contextvars.ContextVar("finagent_event_queue", default=None)
)


def _result_preview(result: Any, limit: int = 1200) -> str:
    """把 MCPToolCallResult 抠出可读预览。

    不同 server / 不同模型返回的 content 可能是 str / list[str|dict] / dict / 其它。
    这里走宽松路径：能拿到 text block 就拼，其余兜底成 repr，然后截断。
    """
    content = getattr(result, "content", result)
    if isinstance(content, str):
        return content[:limit]
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                btype = block.get("type")
                if btype == "text":
                    parts.append(str(block.get("text", "")))
                else:
                    parts.append(f"[{btype} omitted]")
            else:
                parts.append(str(block))
        return ("\n".join(p for p in parts if p))[:limit]
    return str(content)[:limit]


async def log_tool_call(
    request: MCPToolCallRequest,
    handler: Callable[[MCPToolCallRequest], Awaitable[MCPToolCallResult]],
) -> MCPToolCallResult:
    """工具调用拦截器：CLI 端 print；WS 端把事件 push 到当前 task 的事件队列。"""
    queue = current_event_queue.get()
    if queue is not None:
        # WebSocket 路径：结构化事件，不打到 server stdout
        await queue.put(
            {
                "type": "tool_call",
                "server": request.server_name,
                "name": request.name,
                "args": request.args,
            }
        )
        try:
            result = await handler(request)
        except Exception as e:
            await queue.put(
                {
                    "type": "tool_error",
                    "server": request.server_name,
                    "name": request.name,
                    "error": f"{type(e).__name__}: {e}",
                }
            )
            raise
        await queue.put(
            {
                "type": "tool_result",
                "server": request.server_name,
                "name": request.name,
                "preview": _result_preview(result),
            }
        )
        return result

    # CLI 路径：跟旧版完全一致
    print(f"   [→ MCP:{request.server_name}] {request.name}({request.args})")
    return await handler(request)
def _flatten_block_content(content: Any) -> str:
    """
    国产模型：string，将复杂的块级内容展平为单一的纯文本字符串。
    国外模型：list
    """
    
    # 1. 基础情况：如果传入的 content 已经是字符串，直接返回即可。
    if isinstance(content, str):
        return content
        
    # 2. 列表情况：如果传入的 content 是一个列表（通常包含多个文本片段或结构化字典）。
    if isinstance(content, list):
        provider = os.getenv("LLM_PROVIDER", "default").lower()
        list_format_providers = ["gemini", "openai", "claude"]
        is_foreign_multimodal = provider in list_format_providers
        if is_foreign_multimodal:
            return content
        # 初始化一个类型标注为字符串列表的空列表，用于临时存放提取出的每一段文本
        parts: list[str] = []
        
        # 遍历列表中的每一个元素
        for item in content:
            # 2.1 如果当前元素本身就是字符串，直接追加到 parts 列表中
            if isinstance(item, str):
                parts.append(item)
                
            # 2.2 如果当前元素是一个字典（这通常代表一个带有属性的富文本块，例如 {"type": "text", "text": "你好"}）
            elif isinstance(item, dict):
                # 获取该字典的 "type" 字段，用来判断这个块的类型
                block_type = item.get("type")
                
                # 如果这是一个纯文本块（"text"）
                if block_type == "text":
                    # 提取字典中的 "text" 字段。使用 get 防止 KeyError，默认值设为 ""，并强制转为字符串后追加
                    parts.append(str(item.get("text", "")))
                else:
                    # 如果是其他类型的块（例如 "image", "file", "video" 等），不提取具体内容
                    # 而是生成一个占位符，例如 "[image content omitted]"，表示该非文本内容被省略
                    parts.append(f"[{block_type} content omitted]")
                    
            # 2.3 如果元素既不是纯字符串也不是字典（比如数字、布尔值等未知类型）
            else:
                # 直接将其强制转换为字符串并追加
                parts.append(str(item))
                
        # 遍历结束后，将 parts 列表中的有效片段提取出来（if p 过滤掉空字符串）
        # 使用换行符 "\n" 将它们拼接成一个完整的长字符串
        # 如果拼接结果为空（比如原列表是空的，或者全是空字符串），则使用 or 返回默认提示 "(无文本内容)"
        return "\n".join(p for p in parts if p) or "(无文本内容)"
        
    # 3. 兜底情况：如果传入的 content 既不是字符串，也不是列表（例如直接传入了一个单独的字典、整数或 None 等）
    # 则直接强制将其转换为字符串返回，保证函数始终输出 str 类型
    return str(content)


def _wrap_tool_for_string_content(tool: BaseTool) -> BaseTool:
    """包一层 coroutine，把返回值里的 content 部分压扁成字符串。"""
    if not isinstance(tool, StructuredTool) or tool.coroutine is None:
        return tool

    original = tool.coroutine

    def _format_wrapped_result(content: str, artifact: Any = None) -> Any:
        if tool.response_format == "content_and_artifact":
            return content, artifact
        return content

    async def wrapped(**kwargs: Any) -> Any:
        result = await original(**kwargs)
        # StructuredTool 在 response_format="content_and_artifact" 下返回 (content, artifact)
        if isinstance(result, tuple) and len(result) == 2:
            content, artifact = result
            return _format_wrapped_result(_flatten_block_content(content), artifact)
        return _format_wrapped_result(_flatten_block_content(result))

    return StructuredTool(
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
        coroutine=wrapped,
        response_format=tool.response_format,
        metadata=tool.metadata,
    )

# ------------------------------------------------------------------ #
#  Filesystem
# ------------------------------------------------------------------ #

SENSITIVE_PATH_PATTERNS: list[str] = [
    # 明确要藏的机密 / 配置
    ".env",
    # 记忆图谱：memory MCP 独占，绕过它直接改 JSON 可能破坏 entity/relation 一致性
    "memory.json",
    # 交易 ledger：skills/portfolio.py 独占，必须走 portfolio_* MCP 工具
    # 以保证 append-only + schema 一致。filesystem 绕过写会破坏回放一致性。
    "portfolio.jsonl",
    # 工程噪音：内容巨大 + 对 agent 无信息价值，还可能污染 list_directory 输出
    ".venv",
    ".git",
    "__pycache__",
    # 防御性：未来往项目里放密钥 / 凭证时自动兜底，不用每次都改这个列表
    "*.key",
    "*.pem",
    "id_rsa*",
    "credentials.json",
    "secrets.*",
]


def _is_sensitive_path(path_str: str) -> bool:
    """路径的任何一段匹配任意敏感 pattern 就返回 True。"""
    if not path_str:
        return False
    p = Path(path_str)
    for part in p.parts:
        for pattern in SENSITIVE_PATH_PATTERNS:
            if fnmatch.fnmatch(part, pattern):
                return True
    return False


#: filesystem MCP 里所有带 path 语义的参数名。不同工具命名不统一，全列出来；
#: 新版本万一加了别的（比如 source/destination 的 move_file），补进来即可。
_FILESYSTEM_PATH_ARG_NAMES = ("path", "source", "destination")

def _wrap_filesystem_tool_with_guard(tool: BaseTool) -> BaseTool:
    """给 filesystem 工具加一层路径黑名单守卫。命中就返回一条错误文本，
    让 LLM 看到"被拒绝"这个信号并自己改策略（而不是拿到空内容误以为成功）。"""
    if not isinstance(tool, StructuredTool) or tool.coroutine is None:
        return tool

    original = tool.coroutine
    tool_name = tool.name

    def _format_guard_result(message: str) -> Any:
        if tool.response_format == "content_and_artifact":
            return message, None
        return message

    async def wrapped(**kwargs: Any) -> Any:
        for arg_name in _FILESYSTEM_PATH_ARG_NAMES:
            val = kwargs.get(arg_name)
            if isinstance(val, str) and _is_sensitive_path(val):
                return _format_guard_result(
                    f"[filesystem.{tool_name} 拒绝] 路径 {val!r} 命中敏感黑名单 "
                    f"(SENSITIVE_PATH_PATTERNS)。\n"
                    f"- .env 永远不能碰\n"
                    f"- memory.json 请用 memory MCP 的工具（create_entities / add_observations 等）\n"
                    f"- portfolio.jsonl 请用 portfolio_* 工具（record_trade / get_positions / query_trades 等）\n"
                    f"- .venv / .git / __pycache__ 是工程目录，对你无信息价值"
                )
        return await original(**kwargs)

    return StructuredTool(
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
        coroutine=wrapped,
        response_format=tool.response_format,
        metadata=tool.metadata,
    )

