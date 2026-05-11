"""skills 内部共享的工具函数（不会被自动注册为 MCP tool）。

放这里的东西原则：
- 多个 skill 复用的 HTTP client / 重试逻辑 / 格式化函数
- 不直接对外暴露的纯 helper

文件名以下划线开头，会被 finMCP.py 的自动发现器跳过。
"""

from __future__ import annotations

import requests

from config import settings

FINNHUB_BASE = "https://finnhub.io/api/v1"

_session = requests.Session()


def finnhub_get(path: str, params: dict | None = None, timeout: int = 10) -> dict:
    """对 Finnhub REST API 的薄封装，自动注入 token。

    用法:
        data = finnhub_get("/quote", {"symbol": "AAPL"})
    """
    if not settings.finnhub_api_key:
        raise RuntimeError("未配置 FINNHUB_API_KEY，请检查 .env")
    params = {**(params or {}), "token": settings.finnhub_api_key}
    r = _session.get(f"{FINNHUB_BASE}{path}", params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def fmt_market_cap_million_usd(mc_million: float | None) -> str:
    """Finnhub 的 marketCapitalization 单位是百万美元，统一格式化为人类可读字符串。"""
    if mc_million is None:
        return "未知"
    mc = mc_million * 1_000_000
    if mc >= 1e12:
        return f"{mc / 1e12:.2f} 万亿美元"
    if mc >= 1e8:
        return f"{mc / 1e8:.2f} 亿美元"
    return f"{mc:,.0f} 美元"


def fmt_number(x, digits: int = 2) -> str:
    """安全地把数字格式化为字符串，None 显示为 '未知'。"""
    if x is None:
        return "未知"
    if isinstance(x, (int, float)):
        return f"{x:.{digits}f}"
    return str(x)
