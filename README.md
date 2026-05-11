# finAgent

> 一个跑在你电脑上的个人投研助手 · LangChain + LangGraph + MCP，CLI 与 Web 双形态。
>
> *A personal investing copilot that runs on your machine. Built on LangChain + LangGraph + MCP, with both a CLI and a streaming WebSocket Web UI.*

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

---

## 它是什么

`finAgent` 不是一个"接 ChatGPT 网页 + 调用函数"的玩具。它是一个**有持续记忆、有主动行为、可工具化扩展**的本地 Agent：

- **多 MCP server 聚合**：通过 `MultiServerMCPClient` 一次启动 5 个 MCP server（你自己的 `finMCP` + 官方 `time` / `filesystem` / `memory` + 美联储 `fred`），统一在一个 LLM 上下文里调度。
- **长期记忆 + 短期上下文 + 身份卡**：
  - 长期事实走 Memory MCP 的知识图谱（`memory.json`）
  - 短期对话走 LangGraph Checkpointer（CLI 用 `InMemorySaver`，Web 用 `AsyncSqliteSaver` 跨进程持久化）
  - 用户画像浓缩为 `identity_card.md`（~250 token），启动时通过 `{{IDENTITY_CARD}}` 占位符注入 system prompt
- **结构化交易 ledger**：`skills/portfolio.py` 维护 append-only JSONL ledger，所有"加/减仓"必须走 `record_trade` 工具，不能让 LLM 直接改文件，保证回放一致性。
- **主动开口**：交互模式下 agent 启动时主动问候（kickoff），用户沉默 5 分钟主动戳一下（heartbeat），exit 时基于本次对话沉淀 memory / 写 journal（reflect）。
- **每日仪式**：`--briefing morning|evening|weekend` 加载对应模板，单轮生成一份盘前 / 盘后 / 周末报告后退出。
- **路径黑名单**：filesystem MCP 暴露给 LLM 时叠一层守卫，`.env` / `memory.json` / `portfolio.jsonl` / `.venv` 等敏感路径永远拒绝。
- **CLI 行为字节级保留**：把 CLI 改成 Web 后端时，`stream_query` 拆出 async generator，CLI wrapper 翻译事件回 `print`，旧 `python cli.py` 体验完全不变。

## 架构

```
                         ┌──────────────────────────────────────────┐
                         │  cli.py (stdio)        web/  (browser)   │
                         │     │                     │              │
                         │     ▼                     ▼              │
                         │  stream_query     server/main.py  ◄── REST + WS
                         │     │                     │              │
                         └─────┴──────────┬──────────┴──────────────┘
                                          │
                              utils/agent_core.py
                              stream_query_events()  ← async generator
                                          │
                                          ▼
                              langchain.agents.create_agent(...)
                              │
                  ┌───────────┼──────────────────────────────────┐
                  │           │                                  │
            ChatOpenAI   LangGraph                  MultiServerMCPClient
            (.env 任意   Checkpointer               (tool_interceptors=log_tool_call)
             OpenAI 兼容) (Sqlite/Memory)           │
                                                    │
              ┌─────────────────┬──────────────────┼────────────────┬──────────────────┐
              ▼                 ▼                  ▼                ▼                  ▼
         finMCP (stdio)    time (stdio)       filesystem (npx)  memory (npx)      fred (stdio)
            │                                                         │
            └─ skills/ 下每个 .py 都通过 @mcp.tool() 自动注册
               (finnhub_quote / finnhub_financials / finnhub_news /
                sec_filings / deep_search / portfolio /
                semantic_memory / context_resolver / ...)
```

## 快速开始

### 0. 先决条件

| 工具 | 说明 |
|---|---|
| Python | ≥ 3.11（项目用了 PEP 604 `dict \| None` 等语法） |
| Node.js + npm | 用来跑 `npx -y @modelcontextprotocol/server-filesystem` 和 `server-memory` |
| 一份 OpenAI 兼容的 LLM API key | OpenAI / DeepSeek / Moonshot / 智谱 / 本地 vLLM 都行 |

可选：
- FRED API key（宏观数据，免费申请）
- Finnhub API key（行情）
- Tavily API key（搜索）

### 1. clone + venv + 装依赖

```bash
git clone https://github.com/wusta/finagent.git
cd finagent

# 强烈建议用独立 venv，别污染 anaconda / 系统 Python
python -m venv .venv

# Windows PowerShell
.\.venv\Scripts\Activate.ps1
# Linux / macOS
source .venv/bin/activate

# 用 venv 自己的 pip 装，避免 PATH 解析坑（见下方"已知陷阱"）
.venv/Scripts/python -m pip install -r requirements.txt          # Windows
.venv/bin/python -m pip install -r requirements.txt              # Unix
```

想要 Web 服务：

```bash
python -m pip install -r requirements-server.txt
```

想要本地 embedding（离线 / 零成本）：

```bash
python -m pip install -r requirements-embedding.txt
# 第一次使用前预下载模型，避免对话中卡住
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-zh-v1.5')"
```

或者直接装可编辑模式（推荐）：

```bash
python -m pip install -e ".[web,embedding]"
```

### 2. 配 `.env`

```bash
cp .env.example .env
# 编辑 .env，最少填 LLM_API_KEY（其它可选）
```

### 3. 跑

**CLI 交互模式**（默认）：
```bash
python cli.py
# 或安装后直接：
finagent
```

**Web 服务**（FastAPI + WebSocket）：
```bash
python -m server --reload --port 8000
# 或：
finagent-web --reload
```
另开终端跑前端：
```bash
cd web
npm install
npm run dev
```
打开 `http://localhost:5173`。

**单次提问**：
```bash
python cli.py "查一下 AAPL 的 PE 和最近的财报"
```

**每日仪式**（生成一份盘前/盘后/周末报告后退出）：
```bash
python cli.py --briefing morning
python cli.py --briefing evening
python cli.py --briefing weekend
```

**Debug 模式**（打印每次 MCP 工具调用）：
```bash
python cli.py --debug
```

## 项目结构

```
finagent/
├── cli.py                      # CLI 入口（interactive / briefing / single-shot）
├── finMCP.py                   # 你的私有 MCP server（自动加载 skills/）
├── config.py                   # 所有 .env 读取集中在这里
├── memory_guard.py             # Memory MCP 的中文化 + entity 守卫
├── server/                     # FastAPI + WebSocket 服务端
│   ├── __main__.py             # python -m server 入口
│   ├── main.py                 # FastAPI app + REST + /ws/sessions/{tid}
│   └── session_manager.py      # MCP / tools / executor / SqliteSaver 单例
├── skills/                     # 每个 .py 都是一组 @mcp.tool()
│   ├── finnhub_quote.py        # 实时报价
│   ├── finnhub_financials.py   # 财务报表
│   ├── finnhub_news.py         # 公司新闻
│   ├── sec_filings.py          # SEC 文件
│   ├── deep_search.py          # Tavily 多 query 网搜聚合
│   ├── portfolio.py            # 交易 ledger（append-only JSONL）
│   ├── semantic_memory.py      # memory.json 上的语义检索
│   └── context_resolver.py     # 模糊指代消歧（"我那个英特尔" → INTC）
├── utils/
│   ├── agent_core.py           # build_llm + stream_query[_events]
│   ├── mcp_tools.py            # build_server_configs + gather_tools + log_tool_call
│   ├── interactive_session.py  # kickoff / heartbeat / reflect 三件套
│   ├── prompt_loader.py        # 模板加载 + 启动期 venv 检查
│   └── semantic_index.py       # 本地向量索引（vectors.npy + meta.jsonl）
├── prompts/
│   ├── invest_agent.txt        # 主 system prompt（含 {{IDENTITY_CARD}} 占位）
│   ├── kickoff.txt             # 启动主动开口
│   ├── heartbeat.txt           # 沉默时主动戳
│   ├── reflection.txt          # exit 时收尾沉淀
│   └── briefing/
│       ├── morning.txt
│       ├── evening.txt
│       └── weekend.txt
└── web/                        # 前端（Vite + React + TS + Tailwind）
    ├── package.json
    └── src/
        ├── App.tsx
        ├── hooks/useChatSocket.ts
        ├── store/chatStore.ts
        └── components/
            ├── ChatWindow.tsx
            ├── AssistantBubble.tsx   # markdown + 内嵌工具调用气泡
            ├── ToolCallBubble.tsx    # 可折叠：args / result preview / error
            ├── Sidebar.tsx           # sessions + reports tabs
            └── ...
```

## 配置说明

所有配置都从 `.env` 读，集中在 `config.py` 里转成 dataclass。完整列表见 [`.env.example`](.env.example)。最关键的几个：

| 变量 | 默认 | 说明 |
|---|---|---|
| `LLM_API_KEY` | — | **必填**。OpenAI 兼容服务的 key |
| `LLM_MODEL` | `gpt-4o-mini` | 模型名 |
| `LLM_BASE_URL` | OpenAI 官方 | 改成 `https://api.deepseek.com/v1` 等可切其他服务 |
| `LLM_TOOL_CONTENT_FORMAT` | `string` | 国产端用 `string`，OpenAI / Anthropic 用 `blocks` |
| `AGENT_SYSTEM_PROMPT_FILE` | — | 推荐 `prompts/invest_agent.txt` |
| `AGENT_MAX_HISTORY_MESSAGES` | `16` | 短期上下文裁剪阈值 |
| `EMBEDDING_PROVIDER` | `zhipu` | 或 `local`（用 sentence-transformers） |
| `FRED_API_KEY` | — | 填了才挂 FRED MCP，否则跳过 |
| `FINNHUB_API_KEY` / `TAVILY_API_KEY` | — | 行情 / 网搜，不填对应工具不可用 |

## 几个关键设计

### 1. CLI / Web 共用一个事件流

`utils/agent_core.py` 里 `stream_query_events()` 是 async generator，yield 出 5 类事件：

```
user / assistant_start / token / turn_break / done
```

CLI 端 `stream_query()` 把事件翻译成 `print(...)`（行为跟改造前字节级一致）；Web 端 `server/main.py:chat_ws` 直接把事件 JSON 化推给前端。改一处，两边都受益。

### 2. 工具调用 trace 用 `contextvars` 路由

`utils/mcp_tools.py:log_tool_call` 同一份代码：
- CLI 路径：`current_event_queue.get() is None` → 走 `print`（旧的 `--debug` 体验）
- WS 路径：handler 进来时 `current_event_queue.set(asyncio.Queue())` → 工具事件入队，前端就能看到可折叠的工具调用气泡

这样**单进程多 WebSocket 连接**互不串扰，靠 asyncio 的 `contextvars` 任务级隔离。

### 3. MCP 子进程进程级常驻

服务进程启动时通过 FastAPI lifespan 起一次 `MultiServerMCPClient`，5 个 MCP server 的 stdio 子进程整个进程生命期都在跑。所有 WebSocket 连接共享同一个 `agent_executor`，只用 `thread_id` 区分会话——刷新页面不会重启 MCP。

### 4. 短期 vs 长期记忆分层

| 层 | 介质 | 生命周期 | 用途 |
|---|---|---|---|
| 短期对话 | LangGraph Checkpointer | CLI: 进程内；Web: SQLite 跨进程 | 多轮上下文 |
| 长期事实 | Memory MCP（知识图谱 JSON） | 永久 | 用户偏好、历史结论 |
| 交易 ledger | `data/portfolio.jsonl`（append-only） | 永久 | 持仓回放 |
| 身份卡 | `reports/_profile/identity_card.md` | 永久 | system prompt 注入 |
| 反思日记 | `reports/_journal/YYYY-MM-DD.md` | 永久 | 跨日复盘 |
| 研究报告 | `reports/*.md` | 永久 | LLM 自己写 + 跨会话引用 |
| 语义索引 | `data/memory_index/{vectors.npy, meta.jsonl}` | 跟 memory 联动 | memory 语义检索 |

## 怎么扩展

### 加一个工具（最常见的扩展）

在 `skills/` 新建一个 `.py`：

```python
# skills/my_tool.py
from skills import mcp

@mcp.tool()
def my_indicator(symbol: str) -> dict:
    """计算我自定义的某指标。给 LLM 看的描述写在这里。"""
    # ... 你的逻辑 ...
    return {"symbol": symbol, "value": 42}
```

不用改 `finMCP.py` —— 它会自动 import `skills/` 下所有非下划线开头的模块并触发注册。

### 加一个外部 MCP server

改 `utils/mcp_tools.py:build_server_configs()`：

```python
servers["my_server"] = {
    "transport": "stdio",  # 或 "sse" / "streamable_http" / "websocket"
    "command": "...",
    "args": [...],
}
```

如果想限制暴露给 LLM 的工具子集，加进 `TOOL_ALLOWLIST`。

### 改 system prompt

编辑 `prompts/invest_agent.txt`，里面用 `{{IDENTITY_CARD}}` 标记会在加载时替换为 `reports/_profile/identity_card.md` 的内容。

## 已知陷阱

### `LLM_TOOL_CONTENT_FORMAT` 选错会报 400

国产端（DeepSeek / Moonshot / 智谱 / 通义 / 百川）和 Groq / Together 必须用 `string`，否则会因为 ToolMessage content 不是字符串被拒绝。OpenAI / Anthropic / Azure 可用 `blocks`。

## 路线图

- [ ] 用 `aiosqlite` 直接查 checkpoints 表，列出 SQLite 里所有 thread（目前只列本进程见过的）
- [ ] 历史 ToolMessage 跟前一条 AIMessage.tool_calls 配对合并，让历史里也能渲染工具气泡
- [ ] Web 端画像 / prompts 编辑器（直接改 `prompts/*.txt` 和 `identity_card.md`）
- [ ] Web 端持仓表格视图（解析 `data/portfolio.jsonl`）
- [ ] Docker compose（后端 + 前端 + sqlite volume）

## 致谢

- [LangChain](https://github.com/langchain-ai/langchain) / [LangGraph](https://github.com/langchain-ai/langgraph) —— Agent 框架
- [Model Context Protocol](https://modelcontextprotocol.io/) —— 工具协议
- [`@modelcontextprotocol/server-filesystem`](https://github.com/modelcontextprotocol/servers) / [`server-memory`](https://github.com/modelcontextprotocol/servers) —— 官方 MCP 实现
- [`fred-mcp-server`](https://pypi.org/project/fred-mcp-server/) by floriancaro —— 美联储经济数据
- [Finnhub](https://finnhub.io/) —— 行情数据源
- [Tavily](https://tavily.com/) —— 网搜后端

## License

[MIT](LICENSE) © 2026 wusta
