"""更深入的财务指标 / 财报数据（数据源：Finnhub /stock/metric, /stock/financials-reported）。

TODO 思路:
- get_basic_financials(ticker)
    /stock/metric?metric=all → 一次性拿 ROE / ROA / 毛利率 / 负债率 / 各种增长率
    选 10-15 个最常用的指标格式化输出
- get_earnings(ticker, limit=4)
    /stock/earnings → 最近 4 个季度 EPS 实际 vs 预期
- get_recommendations(ticker)
    /stock/recommendation → 分析师评级分布（buy/hold/sell 各多少家）
"""

from __future__ import annotations

from skills import mcp
# from skills._common import finnhub_get, fmt_number


@mcp.tool()
def get_basic_financials(ticker: str) -> str:
    """获取一家公司的核心财务指标快照：ROE、毛利率、负债率、各种增长率等。"""
    # TODO: 实现
    # data = finnhub_get("/stock/metric", {"symbol": ticker.upper(), "metric": "all"})
    # m = data.get("metric", {})
    # 取常用字段：roeRfy / grossMarginAnnual / totalDebt2EquityAnnual / ...
    return f"(占位) get_basic_financials({ticker}) 还没实现"


@mcp.tool()
def get_earnings(ticker: str, limit: int = 4) -> str:
    """获取最近 N 个季度的 EPS 实际值 vs 预期值，看是否 beat/miss。"""
    # TODO: finnhub_get("/stock/earnings", {"symbol": ticker.upper(), "limit": limit})
    return f"(占位) get_earnings({ticker}, limit={limit}) 还没实现"


@mcp.tool()
def get_analyst_recommendations(ticker: str) -> str:
    """获取分析师评级分布（强烈买入 / 买入 / 持有 / 卖出 / 强烈卖出 各多少家）。"""
    # TODO: finnhub_get("/stock/recommendation", {"symbol": ticker.upper()})
    # 返回最近一期 + 月度趋势
    return f"(占位) get_analyst_recommendations({ticker}) 还没实现"
