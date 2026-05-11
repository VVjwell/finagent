from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import Awaitable, Callable
from typing import Any


async def run_interactive_session(
    agent_executor: Any,
    session_config: dict,
    *,
    stream_query: Callable[..., Awaitable[None]],
    load_kickoff_prompt: Callable[[], str | None],
    load_heartbeat_prompt: Callable[[], str | None],
    heartbeat_interval_seconds: int,
    heartbeat_max_consecutive: int,
    kickoff: bool = True,
    heartbeat: bool = True,
    reflect: bool = True,
    load_reflection_prompt: Callable[[], str | None] | None = None,
    reflection_min_user_turns: int = 2,
) -> None:
    print("进入交互模式，输入 exit / quit 退出；/new 开新会话。\n" + "-" * 40)

    if kickoff:
        kickoff_text = load_kickoff_prompt()
        if kickoff_text:
            try:
                await stream_query(
                    agent_executor, kickoff_text, session_config, show_user=False
                )
            except Exception as e:
                print(f"\n[启动问候失败，跳过] {type(e).__name__}: {e}")

    loop = asyncio.get_running_loop()
    heartbeat_template = load_heartbeat_prompt() if heartbeat else None
    heartbeat_enabled = bool(heartbeat_template) and heartbeat_interval_seconds > 0
    consecutive_heartbeats = 0

    # Phase E：用户真正输入的实质 turn 数（不含 /new、不含空输入、不含 exit 本身）。
    # 到 exit 时如果 >= reflection_min_user_turns 才触发反思。
    user_turns = 0
    # 记录 exit 原因，决定是否走反思分支：
    #   "exit"     — 用户主动输 exit/quit → 走反思
    #   "interrupt" — Ctrl+C / EOF / readline 返回空 → 跳过反思（被打断，别强塞）
    exit_reason = "interrupt"

    def _print_prompt() -> None:
        print("\n> ", end="", flush=True)

    _print_prompt()
    input_task: asyncio.Future | None = loop.run_in_executor(
        None, lambda: sys.stdin.readline()
    )

    while True:
        wait_set: list[asyncio.Future] = [input_task]
        hb_timer: asyncio.Task | None = None
        if heartbeat_enabled and consecutive_heartbeats < heartbeat_max_consecutive:
            hb_timer = asyncio.create_task(asyncio.sleep(heartbeat_interval_seconds))
            wait_set.append(hb_timer)

        try:
            done, _ = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if hb_timer is not None and hb_timer not in done:
            hb_timer.cancel()

        if input_task in done:
            try:
                raw = input_task.result()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if raw == "":
                print()
                break
            q = raw.rstrip("\r\n").strip()
            consecutive_heartbeats = 0
            input_task = None

            if not q:
                _print_prompt()
                input_task = loop.run_in_executor(None, lambda: sys.stdin.readline())
                continue
            if q.lower() in {"exit", "quit", ":q"}:
                exit_reason = "exit"
                break
            if q.lower() in {"/new", ":new"}:
                thread_id = uuid.uuid4().hex[:8]
                session_config = {"configurable": {"thread_id": thread_id}}
                # 新会话相当于重置本次 session 累积状态
                user_turns = 0
                print(f"↺ 新会话 thread_id={thread_id}")
            else:
                try:
                    await stream_query(agent_executor, q, session_config)
                    user_turns += 1
                except Exception as e:
                    print(f"[Error] {type(e).__name__}: {e}")

            _print_prompt()
            input_task = loop.run_in_executor(None, lambda: sys.stdin.readline())
        else:
            try:
                await stream_query(
                    agent_executor,
                    heartbeat_template,
                    session_config,
                    show_user=False,
                )
            except Exception as e:
                print(f"\n[heartbeat 失败] {type(e).__name__}: {e}")
            consecutive_heartbeats += 1
            _print_prompt()

    # ------------------------------------------------------------------ #
    # Phase E · 收尾反思（仅在"用户主动 exit + 实质对话 >= 门槛 + 未禁用"时触发）
    # ------------------------------------------------------------------ #
    should_reflect = (
        reflect
        and exit_reason == "exit"
        and user_turns >= reflection_min_user_turns
        and load_reflection_prompt is not None
    )
    if should_reflect:
        reflection_text = load_reflection_prompt()
        if reflection_text:
            print(f"\n📝 收尾反思中（{user_turns} 轮实质对话）...")
            try:
                await stream_query(
                    agent_executor,
                    reflection_text,
                    session_config,
                    show_user=False,
                )
            except KeyboardInterrupt:
                print("\n[反思被中断，已跳过]")
            except Exception as e:
                print(f"\n[反思失败，已跳过] {type(e).__name__}: {e}")
