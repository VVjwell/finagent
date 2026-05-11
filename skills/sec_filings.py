"""美股 SEC 财报检索（数据源：SEC EDGAR 官方 API，免费无需 key）。

EDGAR API:
    https://data.sec.gov/submissions/CIK{cik}.json
    https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json

注意事项:
- 必须设置真实的 User-Agent，否则 SEC 会 403
  例: "MyApp myname@example.com"
- ticker → CIK 映射文件: https://www.sec.gov/files/company_tickers.json
  建议本地缓存（一天刷新一次）
- 财报正文（10-K / 10-Q）通常很大，最好抽取关键章节而不是整段返回

TODO:
- 实现 ticker → CIK 转换 + 缓存
- get_recent_filings(ticker, form_type='10-K', limit=4)
- get_filing_section(cik, accession, section='Risk Factors')
"""

from __future__ import annotations

from skills import mcp


@mcp.tool()
def get_recent_filings(ticker: str, form_type: str = "10-K", limit: int = 4) -> str:
    """获取一家美股公司最近的 SEC 财报列表。

    参数:
        ticker: 美股代码
        form_type: 财报类型，常用 '10-K'(年报) / '10-Q'(季报) / '8-K'(临时公告)
        limit: 返回条数
    """
    # TODO: 实现 ticker→CIK→submissions 拉取
    return f"(占位) get_recent_filings({ticker}, {form_type}, limit={limit}) 还没实现"
