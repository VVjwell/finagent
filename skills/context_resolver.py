"""Unified context resolver for the agent.

The agent should not decide from scratch whether to read identity_card,
portfolio ledger, or long-term memory. This tool encodes the source priority:

1. Current positions / trades -> portfolio ledger
2. User profile / preferences -> identity card + me observations
3. Market/company research -> memory graph + reports

It is a read-only orchestration layer. Writes still go through portfolio_*,
memory tools, or filesystem tools.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from config import settings
from skills import mcp
from skills.portfolio import (
    _format_positions_report,
    _parse_date,
    _read_ledger,
    _replay_positions,
    _sort_key,
)

ContextIntent = Literal["auto", "portfolio", "user_profile", "research", "full"]

_MAX_OBSERVATIONS = 8
_MAX_EVENTS = 12
_MAX_REPORTS = 8


@dataclass
class MemoryEntity:
    name: str
    entity_type: str
    observations: list[str]


def _clip(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n...（已截断）"


def _tokens(text: str) -> list[str]:
    return [
        token.lower()
        for token in re.findall(r"[A-Za-z0-9.]+|[\u4e00-\u9fff]{2,}", text)
        if len(token.strip()) >= 2 or any(ch.isdigit() for ch in token)
    ]


def _extract_tickers(topic: str) -> list[str]:
    tickers = []
    for token in re.findall(r"\b[A-Z]{1,6}(?:\.[A-Z]{1,3})?\b", topic):
        if token not in {"USD", "ETF", "AI", "Q", "PE"}:
            tickers.append(token)
    return list(dict.fromkeys(tickers))


def _load_memory_entities() -> list[MemoryEntity]:
    path = settings.agent.memory_file
    if not path.exists():
        return []
    entities: list[MemoryEntity] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "entity":
                    continue
                name = obj.get("name")
                if not isinstance(name, str):
                    continue
                obs = obj.get("observations") or []
                entities.append(
                    MemoryEntity(
                        name=name,
                        entity_type=str(obj.get("entityType") or "Unknown"),
                        observations=[o for o in obs if isinstance(o, str)],
                    )
                )
    except OSError:
        return []
    return entities


def _score_text(text: str, terms: list[str]) -> int:
    haystack = text.lower()
    return sum(1 for term in terms if term in haystack)


def _matching_memory(topic: str, *, include_user: bool) -> list[tuple[int, MemoryEntity, list[str]]]:
    terms = _tokens(topic)
    if not terms:
        return []
    matches: list[tuple[int, MemoryEntity, list[str]]] = []
    for entity in _load_memory_entities():
        if entity.name == "me" and not include_user:
            continue
        scored_obs: list[tuple[int, str]] = []
        entity_score = _score_text(entity.name, terms) * 3
        for obs in entity.observations:
            score = _score_text(obs, terms) + entity_score
            if score > 0:
                scored_obs.append((score, obs))
        if scored_obs or entity_score:
            scored_obs.sort(key=lambda item: item[0], reverse=True)
            observations = [obs for _, obs in scored_obs[:_MAX_OBSERVATIONS]]
            matches.append((entity_score + sum(s for s, _ in scored_obs[:3]), entity, observations))
    matches.sort(key=lambda item: item[0], reverse=True)
    return matches[:6]


def _format_profile_context(topic: str) -> str:
    lines = ["## User Profile Context"]
    card_path = settings.agent.identity_card_file
    if card_path.exists():
        card = card_path.read_text(encoding="utf-8").strip()
        if card:
            lines.extend(["### Hot Identity Card", _clip(card, 1600)])

    me_matches = [m for m in _matching_memory(topic or "me", include_user=True) if m[1].name == "me"]
    if me_matches:
        lines.append("### Matching me.observations")
        for _, _, observations in me_matches[:1]:
            for obs in observations[:_MAX_OBSERVATIONS]:
                lines.append(f"- {obs}")
    lines.append("Source priority note: profile context is for preferences and style, not current holdings.")
    return "\n".join(lines)


def _format_trade_event(ev: dict[str, Any]) -> str:
    kind = ev.get("kind", "?")
    ts = str(ev.get("ts", ""))[:19]
    ticker = ev.get("ticker", "")
    if kind == "TRADE":
        return (
            f"[{ts}] TRADE {ev.get('side')} {ev.get('qty')} {ticker} "
            f"@ {ev.get('price')} id={ev.get('id')}"
        )
    if kind == "OPTION":
        premium = f" premium={ev.get('premium')}" if ev.get("premium") is not None else ""
        return (
            f"[{ts}] OPTION {ev.get('side')} {ev.get('qty')}x {ticker} "
            f"{ev.get('contract')} strike={ev.get('strike')} exp={ev.get('expiry')}"
            f"{premium} id={ev.get('id')}"
        )
    if kind == "POSITION":
        cost = (
            f" cost_basis={ev.get('cost_basis')}"
            if ev.get("cost_basis") is not None
            else " cost_basis=待补"
        )
        return f"[{ts}] POSITION {ticker} qty={ev.get('qty')}{cost} id={ev.get('id')}"
    if kind == "SNAPSHOT":
        return f"[{ts}] SNAPSHOT {len(ev.get('positions') or [])} positions id={ev.get('id')}"
    return f"[{ts}] {kind} {json.dumps(ev, ensure_ascii=False)}"


def _active_ledger_events(events: list[dict]) -> list[dict]:
    """Return events after applying append-only corrections for context display."""
    corrections: dict[str, dict | None] = {}
    for ev in events:
        if ev.get("kind") == "CORRECTION":
            corrections[str(ev.get("supersedes", ""))] = ev.get("replacement")

    active: list[dict] = []
    for ev in events:
        if ev.get("kind") == "CORRECTION":
            continue
        ev_id = str(ev.get("id", ""))
        if ev_id in corrections:
            replacement = corrections[ev_id]
            if replacement:
                active.append(replacement)
            continue
        active.append(ev)
    return active


def _format_portfolio_context(topic: str) -> str:
    events = _read_ledger()
    lines = [
        "## Portfolio Context",
        "Authority: portfolio ledger is the source of truth for current holdings, trades, options, and cash.",
    ]
    if not events:
        lines.append("Ledger is empty.")
        return "\n".join(lines)

    state = _replay_positions(events)
    lines.append(_format_positions_report(state, as_of_label="today"))
    active_events = _active_ledger_events(events)

    tickers = _extract_tickers(topic)
    if tickers:
        wanted = set(tickers)
        matched = [
            ev for ev in active_events
            if str(ev.get("ticker", "")).upper() in wanted
        ]
    else:
        terms = _tokens(topic)
        matched = [
            ev for ev in active_events
            if _score_text(json.dumps(ev, ensure_ascii=False), terms) > 0
        ] if terms else active_events

    if matched:
        matched.sort(key=_sort_key, reverse=True)
        lines.append("### Relevant Ledger Events")
        for ev in matched[:_MAX_EVENTS]:
            lines.append(f"- {_format_trade_event(ev)}")
    return "\n".join(lines)


def _report_candidates(topic: str) -> list[Path]:
    reports_dir = settings.agent.reports_dir
    if not reports_dir.exists():
        return []
    terms = _tokens(topic)
    candidates: list[tuple[int, Path]] = []
    for path in reports_dir.rglob("*.md"):
        if any(part.startswith("_sandbox") for part in path.parts):
            continue
        name = path.name.lower()
        score = _score_text(name, terms)
        if score:
            candidates.append((score, path))
    candidates.sort(key=lambda item: (item[0], item[1].stat().st_mtime), reverse=True)
    return [path for _, path in candidates[:_MAX_REPORTS]]


def _format_research_context(topic: str) -> str:
    lines = [
        "## Research Context",
        "Authority: market/company facts here are background; current holdings still come from portfolio ledger.",
    ]
    matches = _matching_memory(topic, include_user=False)
    if matches:
        lines.append("### Matching Memory Entities")
        for _, entity, observations in matches:
            lines.append(f"#### {entity.name} ({entity.entity_type})")
            if observations:
                for obs in observations:
                    lines.append(f"- {obs}")
            else:
                lines.append("- 命中实体名，但没有匹配 observation。")

    reports = _report_candidates(topic)
    if reports:
        lines.append("### Related Reports")
        for path in reports:
            rel = path.relative_to(settings.agent.workspace_dir)
            lines.append(f"- {rel}")

    if len(lines) == 2:
        lines.append("No matching research memory or reports found.")
    return "\n".join(lines)


def _resolve_auto_intent(topic: str) -> ContextIntent:
    lowered = topic.lower()
    portfolio_markers = ("持仓", "仓位", "股", "成本", "交易", "买了", "卖了", "put", "call", "option", "ledger")
    profile_markers = ("我对", "我的风格", "风险", "偏好", "原则", "画像", "以前怎么想")
    if any(marker in lowered for marker in portfolio_markers):
        return "portfolio"
    if any(marker in lowered for marker in profile_markers):
        return "user_profile"
    return "research"


@mcp.tool()
def get_context(
    topic: str,
    intent: ContextIntent = "auto",
    max_chars: int = 6000,
) -> str:
    """统一获取回答所需上下文，避免 memory / ledger / identity_card 互相打架。

    Source priority:
      · portfolio: 当前持仓、交易、期权、现金的唯一权威入口
      · user_profile: 用户偏好、风险观、长期原则；不代表当前持仓
      · research: 公司/宏观/行业事实和历史研究材料

    Args:
        topic: 当前问题或主题，例如 "INTC 200股 put策略"、"牧原股份猪周期"
        intent: auto / portfolio / user_profile / research / full
        max_chars: 返回文本最大长度，默认 6000
    """
    clean_topic = topic.strip()
    if not clean_topic:
        return "get_context 需要非空 topic。"

    resolved = _resolve_auto_intent(clean_topic) if intent == "auto" else intent
    sections: list[str] = [f"# Context Pack: {clean_topic}", f"Intent: {resolved}"]

    if resolved == "portfolio":
        sections.append(_format_portfolio_context(clean_topic))
    elif resolved == "user_profile":
        sections.append(_format_profile_context(clean_topic))
    elif resolved == "research":
        sections.append(_format_research_context(clean_topic))
    elif resolved == "full":
        sections.extend(
            [
                _format_portfolio_context(clean_topic),
                _format_profile_context(clean_topic),
                _format_research_context(clean_topic),
            ]
        )
    else:
        return f"未知 intent={intent!r}，请用 auto / portfolio / user_profile / research / full。"

    sections.append(
        "## Agent Rule\n"
        "If the user corrects a current holding, treat the correction as highest priority and "
        "write it with portfolio_record_position before asking for optional details like cost basis."
    )
    return _clip("\n\n".join(sections), max(1000, min(max_chars, 12000)))
