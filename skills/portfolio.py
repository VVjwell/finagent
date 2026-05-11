"""结构化持仓/交易 ledger（Phase A）。

数据模型：data/portfolio.jsonl，每行一个事件，append-only。几种事件：
  · TRADE     股票买/卖
  · OPTION    期权开/平仓
  · SNAPSHOT  持仓快照（迁移起点 / 定期校准；会重置当时的持仓状态）
  · POSITION  单标的当前股数校准（不重置整个组合；适合用户只确认"现在持有 N 股"）
  · CASH      现金变动（存/取款、分红、利息）

设计原则：
  1. append-only。历史永不改；写错了用 CORRECTION 事件指向旧 id 覆盖。
  2. 持仓状态**完全由 ledger 回放得出**，不单独持久化 positions.json。
     avoids "ledger 与 snapshot 打架" 这类一致性灾难。
  3. 时间锚定：每个事件强制带 ts（ISO 8601 字符串）。读的时候按 ts 排序再回放，
     保证无论写入顺序如何，结果一致。
  4. 成本基：买入用移动加权平均；卖出扣 qty 不动 cost_basis；全部卖完即清仓。
     realized_pnl 累计到 ticker 上，方便事后复盘。
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from skills import mcp
from config import settings


LEDGER_PATH: Path = settings.agent.portfolio_file


# ------------------------------------------------------------------ #
#  底层工具
# ------------------------------------------------------------------ #

def _read_ledger() -> list[dict]:
    """读 ledger 文件，返回事件列表。文件不存在 / 单行坏 → 跳过，不崩。"""
    if not LEDGER_PATH.exists():
        return []
    events: list[dict] = []
    with open(LEDGER_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # 坏行忽略，不影响整体
    return events


def _append_event(event: dict) -> None:
    """append 一行 JSON 到 ledger。父目录不存在就建。"""
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def _now_iso() -> str:
    """带本地时区的 ISO 8601 字符串，精确到秒。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _make_id(kind: str, ts: str, ticker: str = "") -> str:
    """稳定 id：kind 首字母 + 紧凑时间戳 + ticker。碰撞概率足够小。"""
    compact_ts = re.sub(r"[^\d]", "", ts)[:14] or "now"
    slug = ticker.lower() or "x"
    return f"{kind[0].lower()}_{compact_ts}_{slug}"


def _parse_date(ts_str: str | None) -> date | None:
    """容错地从 'YYYY-MM-DD' 或 'YYYY-MM-DDTHH:MM:SS+zz' 里抽日期。"""
    if not ts_str:
        return None
    try:
        return date.fromisoformat(ts_str[:10])
    except (ValueError, TypeError):
        return None


def _sort_key(event: dict) -> str:
    """按 ts 升序；同 ts 的保持文件内顺序（稳定排序 + fallback）。"""
    return event.get("ts") or ""


# ------------------------------------------------------------------ #
#  回放：ledger → 持仓状态
# ------------------------------------------------------------------ #

def _replay_positions(events: list[dict], as_of: date | None = None) -> dict[str, Any]:
    """把事件列表按 ts 排序后回放，得到 as_of 时间点的持仓状态。

    返回结构：
        {
            "positions": {
                ticker: {"qty": float, "cost_basis": float, "realized_pnl": float},
                ...
            },
            "cash": float,
            "options": [active option records...],
            "events_applied": int,
        }
    """
    # 先扫一遍 CORRECTION，记下 {supersedes_id: replacement_event}
    corrections: dict[str, dict | None] = {}
    for ev in events:
        if ev.get("kind") == "CORRECTION":
            corrections[ev.get("supersedes", "")] = ev.get("replacement")

    sorted_events = sorted(events, key=_sort_key)

    positions: dict[str, dict[str, float]] = defaultdict(
        lambda: {"qty": 0.0, "cost_basis": 0.0, "realized_pnl": 0.0}
    )
    cash: float = 0.0
    options: list[dict] = []
    applied = 0

    for ev in sorted_events:
        kind = ev.get("kind")
        if kind == "CORRECTION":
            continue

        # 替换：如果当前 event 被 CORRECTION 标为过期，用 replacement
        if ev.get("id") in corrections:
            ev = corrections[ev["id"]] or {}
        if not ev:
            continue

        ev_date = _parse_date(ev.get("ts"))
        if as_of and ev_date and ev_date > as_of:
            break  # 已排序，后面的更新

        if kind == "SNAPSHOT":
            # 快照**重置**持仓——这是快照的语义。迁移场景常用。
            positions.clear()
            for pos in ev.get("positions") or []:
                t = str(pos.get("ticker", "")).upper()
                if not t:
                    continue
                positions[t] = {
                    "qty": float(pos.get("qty", 0) or 0),
                    "cost_basis": float(pos.get("cost_basis", 0) or 0),
                    "realized_pnl": 0.0,
                }
            if ev.get("cash") is not None:
                cash = float(ev["cash"])
            applied += 1

        elif kind == "TRADE":
            t = str(ev.get("ticker", "")).upper()
            side = str(ev.get("side", "")).upper()
            qty = float(ev.get("qty", 0) or 0)
            price = float(ev.get("price", 0) or 0)
            if not t or qty <= 0:
                continue
            p = positions[t]
            if side == "BUY":
                old_qty = p["qty"]
                new_qty = old_qty + qty
                if new_qty > 0:
                    p["cost_basis"] = (
                        p["cost_basis"] * old_qty + price * qty
                    ) / new_qty
                p["qty"] = new_qty
                cash -= qty * price
            elif side == "SELL":
                realized = (price - p["cost_basis"]) * qty
                p["realized_pnl"] += realized
                p["qty"] -= qty
                cash += qty * price
                if abs(p["qty"]) < 1e-9:
                    p["qty"] = 0.0
            applied += 1

        elif kind == "POSITION":
            t = str(ev.get("ticker", "")).upper()
            if not t:
                continue
            qty = float(ev.get("qty", 0) or 0)
            p = positions[t]
            p["qty"] = qty
            if ev.get("cost_basis") is not None:
                p["cost_basis"] = float(ev.get("cost_basis", 0) or 0)
            # 这是持仓数量校准，不推导现金变动；现金仍以 TRADE / CASH / SNAPSHOT 为准。
            applied += 1

        elif kind == "OPTION":
            options.append(ev)
            # 期权对现金的影响：收权利金 +premium * qty * 100，付权利金 -
            premium = ev.get("premium")
            op_side = str(ev.get("side", "")).upper()
            if premium is not None:
                multiplier = float(ev.get("qty", 1) or 1) * 100
                if op_side.startswith("SELL"):
                    cash += float(premium) * multiplier
                elif op_side.startswith("BUY"):
                    cash -= float(premium) * multiplier
            applied += 1

        elif kind == "CASH":
            cash += float(ev.get("delta", 0) or 0)
            applied += 1

    # 清掉已卖光的持仓
    active_positions = {
        t: p for t, p in positions.items() if abs(p["qty"]) > 1e-9
    }
    return {
        "positions": active_positions,
        "cash": cash,
        "options": options,
        "events_applied": applied,
    }


def _format_positions_report(state: dict[str, Any], as_of_label: str) -> str:
    """把回放结果渲染成人类可读的 markdown-ish 表格。"""
    positions: dict[str, dict] = state["positions"]
    cash: float = state["cash"]
    options: list[dict] = state["options"]

    lines = [f"【持仓快照 as_of={as_of_label}】"]
    if not positions:
        lines.append("  （股票仓位为空）")
    else:
        lines.append("  股票持仓:")
        total_cost = 0.0
        for t in sorted(positions):
            p = positions[t]
            qty = p["qty"]
            cb = p["cost_basis"]
            position_cost = qty * cb
            total_cost += position_cost
            pnl = p["realized_pnl"]
            pnl_str = f"  已实现 P&L {pnl:+.2f}" if abs(pnl) > 0.01 else ""
            lines.append(
                f"    · {t:<6} {qty:>8g} 股  @ 均成本 {cb:>10.4f}"
                f"  仓位成本 ${position_cost:>12,.2f}{pnl_str}"
            )
        lines.append(f"  股票仓位成本合计: ${total_cost:,.2f}")

    lines.append(f"  现金 / 等价物     : ${cash:,.2f}")
    total = sum(p["qty"] * p["cost_basis"] for p in positions.values()) + cash
    lines.append(f"  账户总成本基      : ${total:,.2f}")

    # 只列活跃（未到期）期权
    today = date.today()
    active_options = []
    for op in options:
        exp = _parse_date(op.get("expiry"))
        if exp is None or exp >= today:
            active_options.append(op)
    if active_options:
        lines.append("  活跃期权:")
        for op in active_options:
            exp_str = op.get("expiry", "")
            lines.append(
                f"    · {op.get('side','?')} {op.get('qty',1)}x "
                f"{op.get('ticker','?')} {op.get('contract','?')} "
                f"strike={op.get('strike','?')} exp={exp_str}"
                + (
                    f"  premium={op.get('premium')}"
                    if op.get("premium") is not None
                    else ""
                )
            )
    lines.append(f"  （基于 {state['events_applied']} 条 ledger 事件回放得出）")
    return "\n".join(lines)


# ------------------------------------------------------------------ #
#  MCP 工具
# ------------------------------------------------------------------ #

@mcp.tool()
def portfolio_record_trade(
    ticker: str,
    side: str,
    qty: float,
    price: float,
    ts: str | None = None,
    rationale: str | None = None,
    tags: list[str] | None = None,
    currency: str = "USD",
) -> str:
    """记录一笔股票交易（买 or 卖）到 data/portfolio.jsonl。

    这是**记录用户真实交易**的唯一正规入口。请在用户明确提到已执行的交易时调用，
    不要把"打算买"、"考虑卖"之类的意向写进来（ledger 是事实记录，不是意向表）。

    参数:
        ticker   : 股票代码，如 'INTC'、'MSFT'
        side     : 'BUY' 或 'SELL'
        qty      : 股数（允许小数，比如零股/碎股）
        price    : 每股成交价
        ts       : ISO 8601 时间字符串，如 '2026-04-20T14:32:00'；省略则取当前时间
        rationale: 交易理由；强烈建议填，日后复盘时价值巨大
        tags     : 标签数组，如 ['covered-call-hedge', 'risk-reduction', 'earnings-play']
        currency : 默认 'USD'
    """
    side_u = side.upper().strip()
    if side_u not in {"BUY", "SELL"}:
        return f"❌ side 只能是 BUY 或 SELL，你给的是 {side!r}"
    ticker_u = ticker.upper().strip()
    if not ticker_u:
        return "❌ ticker 不能为空"
    try:
        qty_f = float(qty)
        price_f = float(price)
    except (TypeError, ValueError):
        return f"❌ qty/price 必须是数字，你给的是 qty={qty!r}, price={price!r}"
    if qty_f <= 0 or price_f <= 0:
        return "❌ qty 和 price 必须为正数"

    ts_final = ts or _now_iso()
    event: dict[str, Any] = {
        "kind": "TRADE",
        "id": _make_id("TRADE", ts_final, ticker_u),
        "ts": ts_final,
        "ticker": ticker_u,
        "side": side_u,
        "qty": qty_f,
        "price": price_f,
        "currency": currency.upper(),
    }
    if rationale:
        event["rationale"] = rationale.strip()
    if tags:
        event["tags"] = [str(t).strip() for t in tags if str(t).strip()]
    _append_event(event)

    state = _replay_positions(_read_ledger())
    pos = state["positions"].get(ticker_u)
    if pos:
        pos_line = (
            f"{ticker_u}: {pos['qty']:g} 股 @ 均成本 {pos['cost_basis']:.4f}"
        )
        if abs(pos["realized_pnl"]) > 0.01:
            pos_line += f"  (历史已实现 P&L {pos['realized_pnl']:+.2f})"
    else:
        pos_line = f"{ticker_u}: 已清仓"

    return (
        f"✓ 已记录 {side_u} {qty_f:g} {ticker_u} @ {price_f}\n"
        f"  id     : {event['id']}\n"
        f"  ts     : {ts_final}\n"
        f"  现金变动: {'+' if side_u == 'SELL' else '-'}${qty_f * price_f:,.2f}\n"
        f"  最新持仓: {pos_line}"
    )


@mcp.tool()
def portfolio_record_option(
    ticker: str,
    side: str,
    contract: str,
    strike: float,
    expiry: str,
    qty: int = 1,
    premium: float | None = None,
    ts: str | None = None,
    rationale: str | None = None,
) -> str:
    """记录一笔期权交易。

    参数:
        ticker  : 底层标的，如 'INTC'
        side    : 'BUY_TO_OPEN' / 'SELL_TO_OPEN' / 'BUY_TO_CLOSE' / 'SELL_TO_CLOSE'
        contract: 'CALL' 或 'PUT'
        strike  : 行权价
        expiry  : 到期日，ISO 日期字符串 'YYYY-MM-DD'
        qty     : 合约张数，默认 1（1 张 = 100 股）
        premium : 单股权利金（可选；给了就自动算现金流：卖权利金入账、买权利金出账）
        ts      : 开仓/平仓时间；省略 = 当前时间
        rationale: 策略意图，如 "对冲 INTC 底仓风险"
    """
    side_u = side.upper().strip()
    valid_sides = {"BUY_TO_OPEN", "SELL_TO_OPEN", "BUY_TO_CLOSE", "SELL_TO_CLOSE"}
    if side_u not in valid_sides:
        return f"❌ side 必须是 {sorted(valid_sides)} 之一，你给的是 {side!r}"
    contract_u = contract.upper().strip()
    if contract_u not in {"CALL", "PUT"}:
        return f"❌ contract 只能是 CALL 或 PUT，你给的是 {contract!r}"
    if _parse_date(expiry) is None:
        return f"❌ expiry 必须是 'YYYY-MM-DD' 格式，你给的是 {expiry!r}"

    ts_final = ts or _now_iso()
    ticker_u = ticker.upper().strip()
    event: dict[str, Any] = {
        "kind": "OPTION",
        "id": _make_id("OPTION", ts_final, ticker_u),
        "ts": ts_final,
        "ticker": ticker_u,
        "side": side_u,
        "contract": contract_u,
        "strike": float(strike),
        "expiry": expiry,
        "qty": int(qty),
    }
    if premium is not None:
        event["premium"] = float(premium)
    if rationale:
        event["rationale"] = rationale.strip()
    _append_event(event)

    cash_delta = ""
    if premium is not None:
        delta = float(premium) * int(qty) * 100
        if side_u.startswith("SELL"):
            cash_delta = f"\n  现金变动: +${delta:,.2f}（收权利金）"
        elif side_u.startswith("BUY"):
            cash_delta = f"\n  现金变动: -${delta:,.2f}（付权利金）"
    return (
        f"✓ 已记录期权 {side_u} {qty}x {ticker_u} {contract_u} "
        f"strike={strike} exp={expiry}\n"
        f"  id: {event['id']}" + cash_delta
    )


@mcp.tool()
def portfolio_record_snapshot(
    positions: list[dict],
    cash: float,
    ts: str | None = None,
    note: str | None = None,
) -> str:
    """录入一个持仓快照。

    **快照会重置**其后的持仓计算——它表达的是"这个时间点，账户就是这些持仓 + 这些现金"。
    典型用法：
      · 首次接入账户（冷启动）→ 录一个 SNAPSHOT 作为起点，再往后追 TRADE
      · 定期（季度/年度）账户对账 → 录一个 SNAPSHOT 校准历史积累的漂移

    参数:
        positions: 列表，每项形如 {"ticker": "INTC", "qty": 300, "cost_basis": 58.985}
        cash     : 现金 / 等价物金额（USD）
        ts       : 快照对应的时点；省略 = 当前时间
        note     : 快照备注，如 "从 me.observations 迁移的起始快照"
    """
    if not isinstance(positions, list):
        return "❌ positions 必须是列表"
    cleaned = []
    for p in positions:
        if not isinstance(p, dict):
            return f"❌ positions 每项必须是 dict，发现 {type(p).__name__}: {p}"
        t = str(p.get("ticker", "")).upper().strip()
        if not t:
            continue
        try:
            cleaned.append({
                "ticker": t,
                "qty": float(p.get("qty", 0) or 0),
                "cost_basis": float(p.get("cost_basis", 0) or 0),
            })
        except (TypeError, ValueError):
            return f"❌ positions 里 {t} 的 qty/cost_basis 不是数字"

    ts_final = ts or _now_iso()
    event: dict[str, Any] = {
        "kind": "SNAPSHOT",
        "id": _make_id("SNAPSHOT", ts_final),
        "ts": ts_final,
        "positions": cleaned,
        "cash": float(cash),
    }
    if note:
        event["note"] = note.strip()
    _append_event(event)

    state = _replay_positions(_read_ledger())
    return (
        f"✓ 已录入 SNAPSHOT (id={event['id']})，{len(cleaned)} 个持仓，现金 ${cash:,.2f}\n"
        f"---\n"
        + _format_positions_report(state, as_of_label=ts_final[:10])
    )


@mcp.tool()
def portfolio_record_position(
    ticker: str,
    qty: float,
    cost_basis: float | None = None,
    ts: str | None = None,
    note: str | None = None,
) -> str:
    """校准单个股票的当前持仓数量，不重置整个组合。

    用法场景：
      · 用户纠正："我现在只有 200 股 INTC"、"这个票只剩 50 股了"
      · 用户明确当前股数，但暂时不想/不知道成本基

    这类事实应立即记录，避免下轮继续问同一个股数问题。若 cost_basis 为空，
    工具会保留已有成本基；如果原来没有成本基，则显示为 0，并在 note 里标记待补。
    """
    ticker_u = ticker.upper().strip()
    if not ticker_u:
        return "❌ ticker 不能为空"
    try:
        qty_f = float(qty)
    except (TypeError, ValueError):
        return f"❌ qty 必须是数字，你给的是 {qty!r}"
    if qty_f < 0:
        return "❌ qty 不能为负数"

    ts_final = ts or _now_iso()
    event: dict[str, Any] = {
        "kind": "POSITION",
        "id": _make_id("POSITION", ts_final, ticker_u),
        "ts": ts_final,
        "ticker": ticker_u,
        "qty": qty_f,
    }
    if cost_basis is not None:
        try:
            event["cost_basis"] = float(cost_basis)
        except (TypeError, ValueError):
            return f"❌ cost_basis 必须是数字或留空，你给的是 {cost_basis!r}"
    event["note"] = (note or ("用户确认当前持仓数量；成本基待补" if cost_basis is None else "")).strip()

    _append_event(event)
    state = _replay_positions(_read_ledger())
    pos = state["positions"].get(ticker_u)
    if pos:
        cost_text = (
            f"均成本 {pos['cost_basis']:.4f}"
            if abs(pos["cost_basis"]) > 1e-9
            else "成本基待补"
        )
        current = f"{ticker_u}: {pos['qty']:g} 股，{cost_text}"
    else:
        current = f"{ticker_u}: 已清仓"
    return (
        f"✓ 已校准当前持仓 {ticker_u} = {qty_f:g} 股\n"
        f"  id: {event['id']}\n"
        f"  说明: {event['note'] or '无'}\n"
        f"  最新状态: {current}"
    )


@mcp.tool()
def portfolio_get_positions(as_of: str | None = None) -> str:
    """回放 ledger 得到指定时间点的持仓快照。

    参数:
        as_of: ISO 日期字符串 'YYYY-MM-DD'；省略 = 到今天为止的最新状态。
               想看"一周前的组合是啥"时传一个过去的日期即可。

    返回:
        人类可读的持仓表 + 现金 + 活跃期权。只给**成本基**，不取当前市价
        （用户想要市值请自己再调 finMCP 的报价工具，组合成真正的市值）。
    """
    events = _read_ledger()
    if not events:
        return "（ledger 为空。尚未录入任何 SNAPSHOT 或 TRADE）"
    as_of_date = _parse_date(as_of) if as_of else None
    label = as_of if as_of else "today"
    state = _replay_positions(events, as_of=as_of_date)
    return _format_positions_report(state, as_of_label=label)


@mcp.tool()
def portfolio_query_trades(
    ticker: str | None = None,
    since: str | None = None,
    until: str | None = None,
    side: str | None = None,
    kind: str | None = None,
    limit: int = 50,
) -> str:
    """按条件查询历史交易事件。

    参数（全部可选）:
        ticker: 只看某个 ticker
        since : ISO 日期 'YYYY-MM-DD'，只看这天或之后
        until : ISO 日期，只看这天或之前
        side  : BUY / SELL / SELL_TO_OPEN / ... （大小写不敏感）
        kind  : TRADE / OPTION / SNAPSHOT / CASH
        limit : 最多返回多少条（默认 50，按时间倒序）
    """
    events = _read_ledger()
    since_d = _parse_date(since)
    until_d = _parse_date(until)
    side_u = side.upper().strip() if side else None
    kind_u = kind.upper().strip() if kind else None
    ticker_u = ticker.upper().strip() if ticker else None

    filtered = []
    for ev in events:
        if kind_u and ev.get("kind") != kind_u:
            continue
        if ticker_u and str(ev.get("ticker", "")).upper() != ticker_u:
            continue
        if side_u and str(ev.get("side", "")).upper() != side_u:
            continue
        ev_date = _parse_date(ev.get("ts"))
        if ev_date:
            if since_d and ev_date < since_d:
                continue
            if until_d and ev_date > until_d:
                continue
        filtered.append(ev)

    # 按 ts 倒序
    filtered.sort(key=_sort_key, reverse=True)
    filtered = filtered[: max(1, int(limit))]

    if not filtered:
        return "（未匹配到任何事件）"

    lines = [f"命中 {len(filtered)} 条事件（最多 {limit}，按时间倒序）:"]
    for ev in filtered:
        k = ev.get("kind", "?")
        ts = ev.get("ts", "")[:19]
        tk = ev.get("ticker", "")
        ev_id = ev.get("id", "")
        if k == "TRADE":
            lines.append(
                f"  [{ts}] TRADE  id={ev_id}  {ev.get('side'):<4} {ev.get('qty'):>6g} {tk:<6} "
                f"@ {ev.get('price'):>8.4f}"
                + (f"  // {ev.get('rationale')}" if ev.get("rationale") else "")
            )
        elif k == "OPTION":
            lines.append(
                f"  [{ts}] OPTION id={ev_id}  {ev.get('side'):<14} {ev.get('qty')}x {tk} "
                f"{ev.get('contract')} strike={ev.get('strike')} exp={ev.get('expiry')}"
                + (
                    f"  premium={ev.get('premium')}"
                    if ev.get("premium") is not None
                    else ""
                )
                + (f"  // {ev.get('rationale')}" if ev.get("rationale") else "")
            )
        elif k == "SNAPSHOT":
            n = len(ev.get("positions") or [])
            lines.append(
                f"  [{ts}] SNAPSHOT id={ev_id}  {n} 个持仓  现金 ${ev.get('cash', 0):,.2f}"
                + (f"  // {ev.get('note')}" if ev.get("note") else "")
            )
        elif k == "POSITION":
            lines.append(
                f"  [{ts}] POSITION id={ev_id}  {tk:<6} qty={ev.get('qty'):g}"
                + (
                    f"  cost_basis={ev.get('cost_basis'):.4f}"
                    if ev.get("cost_basis") is not None
                    else "  cost_basis=待补"
                )
                + (f"  // {ev.get('note')}" if ev.get("note") else "")
            )
        elif k == "CASH":
            lines.append(
                f"  [{ts}] CASH   id={ev_id}  delta ${ev.get('delta', 0):+,.2f}"
                + (f"  // {ev.get('note')}" if ev.get("note") else "")
            )
        else:
            lines.append(f"  [{ts}] {k}  {json.dumps(ev, ensure_ascii=False)}")
    return "\n".join(lines)


@mcp.tool()
def portfolio_correct_event(
    supersedes_id: str,
    reason: str,
    ts: str | None = None,
) -> str:
    """把一条错误 ledger 事件作废（append-only 纠错，不物理删除）。

    用法场景：
      · 误把"打算买"记成"已成交"
      · 录错方向/价格/数量，但暂时不想补 replacement

    机制：
      · 追加一条 kind=CORRECTION 事件，指向 supersedes_id
      · replacement 固定为 null，表示把原事件从回放中剔除
      · 历史仍可审计，满足 append-only 原则
    """
    target = (supersedes_id or "").strip()
    if not target:
        return "❌ supersedes_id 不能为空"
    why = (reason or "").strip()
    if not why:
        return "❌ reason 不能为空（请写明为什么作废）"

    events = _read_ledger()
    target_event = next((e for e in events if str(e.get("id", "")).strip() == target), None)
    if target_event is None:
        return f"❌ 未找到 id={target!r} 的原始事件；先用 portfolio_query_trades 查到准确 id 再重试"

    ts_final = ts or _now_iso()
    correction: dict[str, Any] = {
        "kind": "CORRECTION",
        "id": _make_id("CORRECTION", ts_final, str(target_event.get("ticker", ""))),
        "ts": ts_final,
        "supersedes": target,
        "replacement": None,
        "reason": why,
    }
    _append_event(correction)

    state = _replay_positions(_read_ledger())
    return (
        f"✓ 已作废事件 id={target}\n"
        f"  correction_id: {correction['id']}\n"
        f"  reason       : {why}\n"
        f"---\n"
        + _format_positions_report(state, as_of_label=ts_final[:10])
    )
