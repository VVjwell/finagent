"""FastAPI 入口：REST 管会话/历史/报告，WebSocket 喷流式事件给前端。

启动：
    uvicorn server.main:app --reload --port 8000
或：
    python -m server          （走 __main__.py）
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import settings
from server.session_manager import SessionManager
from utils.agent_core import stream_query_events
from utils.mcp_tools import current_event_queue
from utils.prompt_loader import (
    BRIEFING_KINDS,
    load_briefing_prompt,
    load_heartbeat_prompt,
    load_kickoff_prompt,
    load_reflection_prompt,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
log = logging.getLogger("finagent.server")


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with AsyncExitStack() as stack:
        sm = SessionManager()
        await sm.start(stack, debug=False)
        app.state.sm = sm
        log.info("finAgent server ready (tools=%d)", len(sm.tools))
        yield
        log.info("finAgent server shutting down")


app = FastAPI(title="finAgent", lifespan=lifespan)

# 本机开发场景前端跑 5173，后端跑 8000，全开就够；正式部署再收紧
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------ #
#  REST: sessions
# ------------------------------------------------------------------ #


class CreateSessionResponse(BaseModel):
    thread_id: str


@app.post("/api/sessions", response_model=CreateSessionResponse)
async def create_session() -> CreateSessionResponse:
    sm: SessionManager = app.state.sm
    return CreateSessionResponse(thread_id=sm.new_thread_id())


@app.get("/api/sessions")
async def list_sessions() -> dict:
    sm: SessionManager = app.state.sm
    return {"threads": sm.list_threads()}


@app.get("/api/sessions/{thread_id}/messages")
async def get_messages(thread_id: str) -> dict:
    sm: SessionManager = app.state.sm
    sm.register_thread(thread_id)
    return {"messages": await sm.get_history(thread_id)}


# ------------------------------------------------------------------ #
#  REST: reports / journal
# ------------------------------------------------------------------ #


_REPORT_KINDS = {
    "daily": lambda: settings.agent.reports_dir / "daily",
    "journal": lambda: settings.agent.reports_dir / "_journal",
    "root": lambda: settings.agent.reports_dir,
}


@app.get("/api/reports/{kind}")
async def list_reports(kind: str) -> dict:
    if kind not in _REPORT_KINDS:
        raise HTTPException(404, "unknown report kind")
    base = _REPORT_KINDS[kind]()
    if not base.exists():
        return {"items": []}
    items = []
    for p in sorted(base.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
        st = p.stat()
        items.append({"name": p.name, "size": st.st_size, "modified": int(st.st_mtime)})
    return {"items": items}


@app.get("/api/reports/{kind}/{name}")
async def read_report(kind: str, name: str) -> dict:
    if kind not in _REPORT_KINDS:
        raise HTTPException(404, "unknown report kind")
    base = _REPORT_KINDS[kind]().resolve()
    target = (base / name).resolve()
    # 防 ../ 越界
    try:
        target.relative_to(base)
    except ValueError:
        raise HTTPException(403, "path escapes base")
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "not found")
    return {"name": name, "content": target.read_text(encoding="utf-8")}


# ------------------------------------------------------------------ #
#  REST: prompts (kickoff/heartbeat/reflection/briefing)
# ------------------------------------------------------------------ #


@app.get("/api/prompts/{kind}")
async def get_prompt(kind: str) -> dict:
    loaders = {
        "kickoff": load_kickoff_prompt,
        "heartbeat": load_heartbeat_prompt,
        "reflection": load_reflection_prompt,
    }
    if kind in loaders:
        return {"text": loaders[kind]() or ""}
    if kind in BRIEFING_KINDS:
        return {"text": load_briefing_prompt(kind)}
    raise HTTPException(404, "unknown prompt kind")


@app.get("/api/health")
async def health() -> dict:
    sm: SessionManager = app.state.sm
    return {
        "ok": sm.agent_executor is not None,
        "tools": len(sm.tools or []),
        "threads_known": len(sm.list_threads()),
    }


# ------------------------------------------------------------------ #
#  WebSocket: 双向聊天
# ------------------------------------------------------------------ #
#
# 前端发：  {"kind": "user"|"kickoff"|"heartbeat"|"reflect"|"briefing",
#             "text": "..." (kind=user 必填，其它可选；空时后端加载对应模板)}
# 后端推：  {"type": "user"|"assistant_start"|"token"|"turn_break"|"done"
#             |"tool_call"|"tool_result"|"tool_error"|"error", ...}


@app.websocket("/ws/sessions/{thread_id}")
async def chat_ws(ws: WebSocket, thread_id: str) -> None:
    await ws.accept()
    sm: SessionManager = ws.app.state.sm
    sm.register_thread(thread_id)
    config = sm.config_for(thread_id)

    out_queue: asyncio.Queue = asyncio.Queue()
    # 让本连接里所有 LangChain 触发的 log_tool_call 都把事件入这条 queue
    cv_token = current_event_queue.set(out_queue)

    pumper_done = asyncio.Event()

    async def pump() -> None:
        try:
            while True:
                ev = await out_queue.get()
                if ev is None:  # 关闭信号
                    return
                try:
                    await ws.send_text(json.dumps(ev, ensure_ascii=False, default=str))
                except Exception:
                    return
        finally:
            pumper_done.set()

    pumper = asyncio.create_task(pump())

    async def run_turn(text: str, *, show_user: bool) -> None:
        try:
            async for ev in stream_query_events(
                sm.agent_executor, text, config, show_user=show_user
            ):
                await out_queue.put(ev)
        except Exception as e:
            log.exception("turn failed")
            await out_queue.put({"type": "error", "error": f"{type(e).__name__}: {e}"})
            # 前端 UI 解锁需要 done
            await out_queue.put({"type": "done", "had_content": False})

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await out_queue.put({"type": "error", "error": "invalid json"})
                continue
            kind = (msg.get("kind") or "user").lower()
            text = (msg.get("text") or "").strip()

            if kind == "user":
                if not text:
                    continue
                await run_turn(text, show_user=True)
            elif kind == "kickoff":
                t = text or (load_kickoff_prompt() or "")
                if t:
                    await run_turn(t, show_user=False)
            elif kind == "heartbeat":
                t = text or (load_heartbeat_prompt() or "")
                if t:
                    await run_turn(t, show_user=False)
            elif kind == "reflect":
                t = text or (load_reflection_prompt() or "")
                if t:
                    await run_turn(t, show_user=False)
            elif kind == "briefing":
                bkind = msg.get("briefing") or "morning"
                if bkind not in BRIEFING_KINDS:
                    await out_queue.put({"type": "error", "error": f"bad briefing: {bkind}"})
                    continue
                await run_turn(load_briefing_prompt(bkind), show_user=False)
            else:
                await out_queue.put({"type": "error", "error": f"unknown kind: {kind}"})
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("ws handler crashed")
    finally:
        await out_queue.put(None)
        try:
            await asyncio.wait_for(pumper_done.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pumper.cancel()
        current_event_queue.reset(cv_token)
