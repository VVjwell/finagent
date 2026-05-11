from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _check_interpreter_is_venv() -> None:
    """启动时提醒：确保跑的是本项目 .venv 的 Python，而不是 anaconda / 系统 Python。

    上一次踩过的坑：终端 venv 激活了但 `python` 仍解析到 anaconda，导致 `langchain`
    是 anaconda 里的那份、`fred-mcp-server` 却只装在 venv 里。打个 warning 就够了，
    真缺模块自然会炸出来。
    """
    venv_path = PROJECT_ROOT / ".venv"
    if not venv_path.exists():
        return
    exe = Path(sys.executable).resolve()
    try:
        exe.relative_to(venv_path)
    except ValueError:
        print(
            f"⚠️  当前 Python 是 {exe}，不在 {venv_path} 里。\n"
            f"    推荐显式用: .\\.venv\\Scripts\\python.exe agent_client.py ...\n"
            f"    或检查 PATH 是否被 anaconda / 其他环境污染。"
        )


#: 合法的 briefing 仪式类型。每个对应 prompts/briefing/<kind>.txt 一份模板文件。
BRIEFING_KINDS = ("morning", "evening", "weekend")


def _load_briefing_prompt(kind: str) -> str:
    """读 prompts/briefing/<kind>.txt；找不到就退出。"""
    path = PROJECT_ROOT / "prompts" / "briefing" / f"{kind}.txt"
    if not path.exists():
        sys.exit(f"找不到 briefing 模板: {path}")
    return path.read_text(encoding="utf-8")


#: 启动问候提示文件。interactive 模式下启动即注入一条隐藏 user turn，
#: agent 走开场仪式后主动开口打招呼。删掉文件或加 --no-kickoff 即可关闭。
KICKOFF_PROMPT_FILE = "prompts/kickoff.txt"

#: Heartbeat 提示文件。用户连续 HEARTBEAT_INTERVAL_SECONDS 秒没响应就注入一条
#: 隐藏 user turn，让 agent 主动戳一下。删掉文件或加 --no-heartbeat 即可关闭。
HEARTBEAT_PROMPT_FILE = "prompts/heartbeat.txt"

#: 会话收尾反思提示（Phase E）。用户输 exit/quit 退出且本次会话有实质交互时，
#: 注入一条隐藏 user turn 让 agent 自己提炼 memory / 写 journal / 更新身份卡。
#: 删掉文件或加 --no-reflect 即可关闭。
REFLECTION_PROMPT_FILE = "prompts/reflection.txt"

#: 触发会话收尾反思的最低门槛（用户真正输入的实质 turn 数）。
#: 低于这个数就直接退出，不强塞反思——只说了"你好"就 exit 没啥可沉淀的。
REFLECTION_MIN_USER_TURNS = 2

#: 默认 heartbeat 触发间隔（秒）。取值的权衡：
#:   · 太短（<5min）：研究场景用户真可能在沉思，agent 一直打断显得烦
#:   · 太长（>30min）：等于没开
#: 15 分钟是比较"像一个同事隔一会儿过来搭话"的体感。
HEARTBEAT_INTERVAL_SECONDS = 5 * 60

#: 用户不响应时最多连续触发多少次 heartbeat。防止用户去开会 2 小时回来看到 10 条消息。
#: 触发到上限后就沉默，用户一开口自动重置计数。
HEARTBEAT_MAX_CONSECUTIVE = 3


def _load_kickoff_prompt() -> str | None:
    """读 prompts/kickoff.txt；文件不存在就返回 None（静默跳过，不当错误）。"""
    path = PROJECT_ROOT / KICKOFF_PROMPT_FILE
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def _load_heartbeat_prompt() -> str | None:
    """读 prompts/heartbeat.txt；文件不存在就返回 None（静默跳过，不当错误）。"""
    path = PROJECT_ROOT / HEARTBEAT_PROMPT_FILE
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def _load_reflection_prompt() -> str | None:
    """读 prompts/reflection.txt；文件不存在就返回 None（静默跳过，不当错误）。"""
    path = PROJECT_ROOT / REFLECTION_PROMPT_FILE
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


# 对外导出的稳定名字（给 cli.py 用）
check_interpreter_is_venv = _check_interpreter_is_venv
load_briefing_prompt = _load_briefing_prompt
load_kickoff_prompt = _load_kickoff_prompt
load_heartbeat_prompt = _load_heartbeat_prompt
load_reflection_prompt = _load_reflection_prompt