"""集中管理所有从 .env 读出的配置。

任何模块只需 `from config import settings`，不再各自调用 os.getenv，
避免配置散落在代码各处。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")


def _get(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _get_float(name: str, default: float) -> float:
    raw = _get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


_VALID_TOOL_CONTENT_FORMATS = {"string", "blocks"}


def _resolve_path(raw: str) -> Path:
    """把用户给的路径解析成绝对路径。相对路径基于项目根目录展开。"""
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p.resolve()


# 向后兼容别名：老代码如果还 import 这俩名字也能正常工作
_resolve_reports_dir = _resolve_path
_resolve_memory_file = _resolve_path


@dataclass(frozen=True)
class EmbeddingSettings:
    """语义记忆索引用的 embedding provider 配置（Phase B）。

    支持两种后端，通过 EMBEDDING_PROVIDER 切换：
      · "zhipu"（默认）：走智谱 OpenAI-compatible 端点，通过 openai SDK 调用
      · "local"：通过 sentence-transformers 加载本地模型（BGE 系列推荐），
                 完全离线、不走网络、零 token 成本；首次使用会从 HuggingFace
                 拉权重到本地缓存（~/.cache/huggingface），之后复用。

    `model` 和 `dim` 永远指向**当前生效**那个 provider 的值，这样
    vectors.npy / config.json 的一致性校验逻辑不用动——切 provider
    等于换模型，原索引会失效，需要 memory_reindex 一次重建。
    """

    provider: str  # "zhipu" | "local"
    # Zhipu 专属（provider=local 时忽略）
    api_key: str
    base_url: str
    # Local 专属（provider=zhipu 时忽略）
    local_device: str | None  # None / "cpu" / "cuda" / "mps"，None = sentence-transformers 自选
    # 当前生效的模型名 & 维度（随 provider 切换）
    model: str
    dim: int
    # 本地向量索引目录（vectors.npy + meta.jsonl + config.json）
    index_dir: Path
    # 单次 embedding / semantic search 最多等待多久，避免工具调用把 agent 卡死。
    timeout_seconds: float
    # 本地模型默认只从缓存加载；未缓存时让用户显式预下载，不在对话中偷偷卡住。
    local_files_only: bool

    @property
    def enabled(self) -> bool:
        # 本地 provider 默认可用；首次 embed 时若 sentence-transformers 未装，
        # 会抛清晰的错误信息由上层捕获，不在这里预判
        if self.provider == "local":
            return True
        return bool(self.api_key)


@dataclass(frozen=True)
class LLMSettings:
    api_key: str
    model: str
    base_url: str | None
    temperature: float
    # MCP 工具返回的 content 如何塞进 ToolMessage：
    #   "string" → 压扁成纯文本。兼容 DeepSeek / Moonshot / 通义 / 智谱 / 百川 等国内端
    #             以及部分 OpenAI 兼容端（Groq、Together）。默认值。
    #   "blocks" → 保留 content blocks 列表（可含图像/文件）。
    #             适用于 OpenAI 官方、Anthropic、Azure OpenAI 等支持多模态 ToolMessage 的端。
    tool_content_format: str

    def require(self) -> None:
        if not self.api_key:
            raise RuntimeError(
                "未设置 LLM_API_KEY。请在项目根目录的 .env 文件里填好后再运行。"
            )
        if self.tool_content_format not in _VALID_TOOL_CONTENT_FORMATS:
            raise RuntimeError(
                f"LLM_TOOL_CONTENT_FORMAT 非法值 '{self.tool_content_format}'，"
                f"合法取值: {sorted(_VALID_TOOL_CONTENT_FORMATS)}"
            )


@dataclass(frozen=True)
class AgentSettings:
    system_prompt_file: str | None
    # 可选，传给 mcp-server-time 的 --local-timezone，例如 "Asia/Shanghai"。
    # 为空时 time server 自动检测系统时区，一般够用。
    local_timezone: str | None
    # Filesystem MCP 的根目录（agent 可见的全部范围）。默认项目根 <repo>/。
    # 这个目录 server 端硬性限制，越界 MCP 自己就拒绝；
    # 目录内的敏感文件（.env / memory.json / .venv / .git / *.key ...）
    # 由 agent_client.py 的 SENSITIVE_PATH_PATTERNS 在工具调用时再拦截一层。
    workspace_dir: Path
    # 默认的报告落盘位置（workspace_dir 的子目录），用于 filesystem.write_file 保存
    # 研究结论 / 数据快照。reports 里的东西是研究产物，便于后续跨会话复用。
    reports_dir: Path
    # Memory MCP 的知识图谱存储文件（绝对路径）。
    # 默认 <repo>/memory.json。跨会话持久，已加入 .gitignore 避免泄露用户偏好。
    # 文件首次写入时自动创建；想清空记忆直接删掉文件即可。
    # 注意：这个文件由 memory MCP 独占读写；filesystem 黑名单里已屏蔽，
    # 避免 LLM 通过 read_file/write_file 直接改破坏图结构。
    memory_file: Path
    # 结构化持仓 / 交易 ledger（append-only JSONL）。
    # 默认 <repo>/data/portfolio.jsonl。由 skills/portfolio.py 独占读写。
    # 同样在 filesystem 黑名单里，强制走 portfolio_* MCP 工具以保证 schema 一致。
    portfolio_file: Path
    # 身份卡（Phase C）：~250 token 浓缩版用户画像，启动时通过
    # {{IDENTITY_CARD}} 占位符注入到 system prompt。
    # agent 通过 filesystem.edit_file 自己维护这份文件。
    identity_card_file: Path
    # 短期会话最多保留多少条消息。长期事实走 memory / ledger，
    # 不让 InMemorySaver 的完整对话历史无限塞回模型上下文。
    max_history_messages: int

    @property
    def system_prompt(self) -> str | None:
        """渲染后的 system prompt。

        支持的占位符：
          {{IDENTITY_CARD}}  → reports/_profile/identity_card.md 的内容；
                               文件不存在 / 为空时替换为一个提示文本，
                               指示 agent 进入 onboarding 流程。
        """
        if not self.system_prompt_file:
            return None
        path = PROJECT_ROOT / self.system_prompt_file
        if not path.exists():
            return None
        raw = path.read_text(encoding="utf-8")
        card = _read_identity_card(self.identity_card_file)
        return raw.replace("{{IDENTITY_CARD}}", card)


_IDENTITY_CARD_EMPTY_MARKER = (
    "（身份卡为空或文件不存在 — 进入 onboarding 模式，按 invest_agent.txt 的 "
    "Onboarding 流程采集主人背景，然后用 filesystem.write_file 创建此卡）"
)


def _read_identity_card(path: Path) -> str:
    """读身份卡文件。不存在或为空时返回 onboarding 占位符。"""
    if not path.exists():
        return _IDENTITY_CARD_EMPTY_MARKER
    text = path.read_text(encoding="utf-8").strip()
    if not text or len(text) < 20:
        # 只有标题几个字的情况也当成空
        return _IDENTITY_CARD_EMPTY_MARKER
    return text


@dataclass(frozen=True)
class Settings:
    finnhub_api_key: str
    # 可选：deep_search 内部使用的 Tavily key。空字符串表示 deep_search 不可用。
    tavily_api_key: str
    # 可选：FRED（美联储经济数据）MCP 的 key。空字符串则跳过该 server
    fred_api_key: str
    llm: LLMSettings
    agent: AgentSettings
    embedding: EmbeddingSettings


def _get_int(name: str, default: int) -> int:
    raw = _get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_bool(name: str, default: bool) -> bool:
    raw = _get(name).lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _load() -> Settings:
    base_url = _get("LLM_BASE_URL")
    return Settings(
        finnhub_api_key=_get("FINNHUB_API_KEY"),
        tavily_api_key=_get("TAVILY_API_KEY"),
        fred_api_key=_get("FRED_API_KEY"),
        llm=LLMSettings(
            api_key=_get("LLM_API_KEY"),
            model=_get("LLM_MODEL", "gpt-4o-mini"),
            base_url=base_url or None,
            temperature=_get_float("LLM_TEMPERATURE", 0.1),
            tool_content_format=_get("LLM_TOOL_CONTENT_FORMAT", "string").lower(),
        ),
        agent=AgentSettings(
            system_prompt_file=_get("AGENT_SYSTEM_PROMPT_FILE") or None,
            local_timezone=_get("AGENT_LOCAL_TIMEZONE") or None,
            workspace_dir=_resolve_path(_get("AGENT_WORKSPACE_DIR") or "."),
            reports_dir=_resolve_path(_get("AGENT_REPORTS_DIR") or "reports"),
            memory_file=_resolve_path(_get("AGENT_MEMORY_FILE") or "memory.json"),
            portfolio_file=_resolve_path(
                _get("AGENT_PORTFOLIO_FILE") or "data/portfolio.jsonl"
            ),
            identity_card_file=_resolve_path(
                _get("AGENT_IDENTITY_CARD_FILE") or "reports/_profile/identity_card.md"
            ),
            max_history_messages=max(0, _get_int("AGENT_MAX_HISTORY_MESSAGES", 16)),
        ),
        embedding=_load_embedding(base_url),
    )


def _load_embedding(llm_base_url: str) -> EmbeddingSettings:
    """按 EMBEDDING_PROVIDER 分支读取配置。默认 "zhipu"。"""
    provider = (_get("EMBEDDING_PROVIDER") or "zhipu").lower()
    if provider not in {"zhipu", "local"}:
        raise RuntimeError(
            f"EMBEDDING_PROVIDER 非法值 '{provider}'，合法取值: ['zhipu', 'local']"
        )

    index_dir = _resolve_path(_get("AGENT_MEMORY_INDEX_DIR") or "data/memory_index")
    timeout_seconds = max(1.0, _get_float("EMBEDDING_TIMEOUT_SECONDS", 8.0))

    if provider == "local":
        return EmbeddingSettings(
            provider="local",
            api_key="",
            base_url="",
            local_device=_get("LOCAL_EMBEDDING_DEVICE") or None,
            # 默认 BGE-small 中文版：~100MB、512 维、CPU 友好；
            # 大模型想换成 BAAI/bge-m3（1024 维，多语言） 或 BAAI/bge-large-zh-v1.5
            model=_get("LOCAL_EMBEDDING_MODEL") or "BAAI/bge-small-zh-v1.5",
            dim=_get_int("LOCAL_EMBEDDING_DIM", 512),
            index_dir=index_dir,
            timeout_seconds=timeout_seconds,
            local_files_only=_get_bool("LOCAL_EMBEDDING_LOCAL_FILES_ONLY", True),
        )

    # zhipu
    # 读 key 的优先级：
    #   1. ZHIPU_API_KEY / ZHIPUAI_API_KEY （显式给 embedding 用）
    #   2. 如果 LLM_BASE_URL 也指向 bigmodel.cn，说明 LLM 本身就走智谱，
    #      embedding 和 LLM 可以共享同一把 key —— 直接 fallback 到 LLM_API_KEY
    api_key = (
        _get("ZHIPU_API_KEY")
        or _get("ZHIPUAI_API_KEY")
        or (
            _get("LLM_API_KEY")
            if "bigmodel.cn" in (llm_base_url or "").lower()
            else ""
        )
    )
    return EmbeddingSettings(
        provider="zhipu",
        api_key=api_key,
        base_url=_get("ZHIPU_BASE_URL") or "https://open.bigmodel.cn/api/paas/v4/",
        local_device=None,
        model=_get("ZHIPU_EMBEDDING_MODEL") or "embedding-3",
        dim=_get_int("ZHIPU_EMBEDDING_DIM", 1024),
        index_dir=index_dir,
        timeout_seconds=timeout_seconds,
        local_files_only=True,
    )


settings = _load()
