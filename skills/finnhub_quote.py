"""股票实时报价、市值、PE 等基础摘要（数据源：Finnhub）。"""

from __future__ import annotations

from skills import mcp
from skills._common import finnhub_get, fmt_market_cap_million_usd, fmt_number


@mcp.tool()
def get_stock_summary(ticker: str) -> str:
    """获取一只股票的实时报价、市值、PE、52周高低、行业等财务摘要。

    参数:
        ticker: 标准股票代码，如 'AAPL'、'NVDA'、'0700.HK'

    免费版 Finnhub 主要覆盖美股，部分港股/欧股也支持，A 股不支持。
    """
    ticker = ticker.upper().strip()

    try:
        quote = finnhub_get("/quote", {"symbol": ticker})
    except Exception as e:
        return f"获取 {ticker} 报价失败: {type(e).__name__}: {e}"

    price = quote.get("c")
    if not price:
        return f"未找到 {ticker} 的行情数据，请检查 Ticker 是否正确。"

    prev_close = quote.get("pc")
    day_high = quote.get("h")
    day_low = quote.get("l")
    change = price - prev_close if prev_close else None
    change_pct = (change / prev_close * 100) if prev_close else None

    profile = {}
    try:
        profile = finnhub_get("/stock/profile2", {"symbol": ticker}) or {}
    except Exception:
        pass

    metrics = {}
    try:
        m = finnhub_get("/stock/metric", {"symbol": ticker, "metric": "all"}) or {}
        metrics = m.get("metric", {}) or {}
    except Exception:
        pass

    name = profile.get("name") or ticker
    industry = profile.get("finnhubIndustry", "未知")
    country = profile.get("country", "未知")
    exchange = profile.get("exchange", "未知")
    market_cap_str = fmt_market_cap_million_usd(profile.get("marketCapitalization"))

    pe_raw = metrics.get("peTTM") or metrics.get("peNormalizedAnnual")
    pe_ratio = fmt_number(pe_raw)
    week52_high = fmt_number(metrics.get("52WeekHigh"))
    week52_low = fmt_number(metrics.get("52WeekLow"))

    change_line = ""
    if change is not None:
        sign = "+" if change >= 0 else ""
        change_line = f"  ({sign}{change:.2f}, {sign}{change_pct:.2f}%)"

    return (
        f"【{name} ({ticker}) 财务摘要】\n"
        f"- 当前价格: {price}{change_line}\n"
        f"- 今日区间: {day_low} ~ {day_high}\n"
        f"- 昨日收盘: {prev_close}\n"
        f"- 52周区间: {week52_low} ~ {week52_high}\n"
        f"- 市值: {market_cap_str}\n"
        f"- 滚动市盈率(PE): {pe_ratio}\n"
        f"- 行业: {industry}\n"
        f"- 交易所: {exchange} ({country})"
    )
