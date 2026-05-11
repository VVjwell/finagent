"""公司相关新闻（数据源：Finnhub /company-news）。

TODO:
- 调用 finnhub_get("/company-news", {"symbol": ticker, "from": ..., "to": ...})
- 返回字段含 headline / summary / source / url / datetime
- 取最新 N 条，拼成 markdown 列表给 LLM
- 注意时间格式：from/to 是 'YYYY-MM-DD' 字符串
"""

from __future__ import annotations

# from datetime import date, timedelta

from skills import mcp
# from skills._common import finnhub_get


@mcp.tool()
def get_company_news(ticker: str, days: int = 7, limit: int = 10) -> str:
    """获取一家公司最近 N 天的相关新闻列表（标题 + 摘要 + 链接）。

    参数:
        ticker: 标准股票代码，如 'AAPL'
        days: 回溯天数，默认 7
        limit: 最多返回多少条，默认 10
    """
    # TODO: 实现
    # today = date.today()
    # since = today - timedelta(days=days)
    # data = finnhub_get("/company-news", {
    #     "symbol": ticker.upper(),
    #     "from": since.isoformat(),
    #     "to": today.isoformat(),
    # })
    # 截取 limit 条 → 格式化字符串
    return f"(占位) get_company_news({ticker}, days={days}, limit={limit}) 还没实现"
