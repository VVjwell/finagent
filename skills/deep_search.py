"""Deep search: multi-query web research with deduped evidence.

这个工具被故意设计为一个“收集器”，而不是第二个智能体（Agent）。
它的职责是发现并提取信息源，然后返回一个紧凑的“证据包”，
供主智能体（Main Agent）进行推理、引用，并可选择性地生成报告。
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests

from config import settings
from skills import mcp

# Tavily 搜索引擎的 API 地址
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
DEFAULT_TIMEOUT_SECONDS = 12 # 默认网络请求超时时间
MAX_TOTAL_RESULTS = 20       # 最终保留的最大搜索结果总数
MAX_CONTENT_CHARS = 1600     # 每条搜索结果正文保留的最大字符数


@dataclass
class SearchResult:
    """定义单条搜索结果的数据结构"""
    title: str      # 网页标题
    url: str        # 网页链接
    content: str    # 网页正文或摘要内容
    score: float    # 搜索引擎给出的相关性打分
    query: str      # 触发这条结果的具体搜索词
    source: str     # 域名来源（如 caixin.com）


def _clean_text(text: str, max_chars: int = MAX_CONTENT_CHARS) -> str:
    """清理文本：将多个连续的空白字符替换为单个空格，并截断超长文本。"""
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if len(text) <= max_chars:
        return text
    # 如果超长，截取前面的部分并加上省略号
    return text[:max_chars].rstrip() + "..."


def _domain(url: str) -> str:
    """从 URL 中提取纯净的域名（去掉协议和 www前缀）。"""
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _normalize_url(url: str) -> str:
    """归一化 URL：去掉 URL 中的锚点（fragment）和查询参数（query），用于后续精准去重。"""
    parsed = urlparse(url)
    return parsed._replace(fragment="", query="").geturl().rstrip("/")


def _build_queries(query: str, max_queries: int) -> list[str]:
    """
    生成确定性的投研子查询（不调用大模型，直接硬编码规则扩展）。
    为了多角度挖掘信息，会自动拼接不同的投研关键词。
    """
    base = query.strip()
    if not base:
        return []

    # 预设的查询扩展模板：涵盖官方、数据、行业影响、风险质疑、机构研报等维度
    candidates = [
        base,
        f"{base} 官方 原文 公告",
        f"{base} 最新 新闻 数据",
        f"{base} 行业 背景 影响",
        f"{base} 风险 反方 质疑",
        f"{base} 研报 分析 机构",
    ]

    out: list[str] = []
    seen: set[str] = set()
    # 按照顺序加入查询，去重，直到达到最大查询数量限制
    for q in candidates:
        key = q.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(q)
        if len(out) >= max_queries:
            break
    return out


def _source_quality(domain: str) -> int:
    """
    轻量级的信息源质量评估器，用于给证据排序（并非绝对的真相保证）。
    给政府网站和知名财经媒体赋予更高的权重。
    """
    # 官方标记域名（权重最高）
    official_markers = (
        "gov.cn", "xinhuanet.com", "people.com.cn", 
        "stats.gov.cn", "mof.gov.cn", "pbc.gov.cn", "sec.gov",
    )
    # 财经标记域名（权重次之）
    finance_markers = (
        "eastmoney.com", "stcn.com", "cs.com.cn", "cnstock.com", 
        "yicai.com", "caixin.com", "reuters.com", "bloomberg.com",
    )
    
    if any(marker in domain for marker in official_markers):
        return 3 # 官方来源给 3 分
    if any(marker in domain for marker in finance_markers):
        return 2 # 财经来源给 2 分
    return 1     # 普通来源给 1 分


def _tavily_search(
    query: str,
    *,
    max_results: int,
    time_range: str | None,
    search_depth: str,
    timeout: int,
) -> list[SearchResult]:
    """单次调用 Tavily API 执行搜索的底层函数。"""
    if not settings.tavily_api_key:
        raise RuntimeError("未配置 TAVILY_API_KEY，deep_search 暂不可用。")

    # 构造请求 API 的参数
    payload: dict[str, Any] = {
        "api_key": settings.tavily_api_key,
        "query": query,
        "max_results": max(1, min(max_results, 10)), # 限制每次最多返回 10 条
        "search_depth": search_depth if search_depth in {"basic", "advanced"} else "advanced",
        "include_answer": False,
        "include_raw_content": True, # 要求返回网页正文
    }
    if time_range:
        payload["time_range"] = time_range

    # 发起 HTTP POST 请求
    response = requests.post(TAVILY_SEARCH_URL, json=payload, timeout=timeout)
    response.raise_for_status()
    data = response.json()

    # 解析返回的 JSON 数据并封装成 SearchResult 对象列表
    results: list[SearchResult] = []
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
            
        # 优先拿 raw_content，没有的话拿普通 content
        raw_content = item.get("raw_content") or item.get("content") or ""
        domain = _domain(url)
        
        results.append(
            SearchResult(
                title=str(item.get("title") or "(untitled)").strip(),
                url=url,
                content=_clean_text(str(raw_content)),
                score=float(item.get("score") or 0.0),
                query=query,
                source=domain,
            )
        )
    return results


def _search_many(
    queries: list[str],
    *,
    max_sources: int,
    time_range: str | None,
    search_depth: str,
    timeout: int,
) -> list[SearchResult]:
    """
    核心并发搜索逻辑：使用多线程跑所有的子查询，然后汇总并去重。
    """
    per_query = max(2, min(6, max_sources)) # 每个子查询尝试获取的结果数
    collected: list[SearchResult] = []

    # 使用线程池并发执行搜索，最大并发数为4或查询数量
    with ThreadPoolExecutor(max_workers=min(4, len(queries))) as executor:
        # 提交所有搜索任务
        futures = [
            executor.submit(
                _tavily_search,
                q,
                max_results=per_query,
                time_range=time_range,
                search_depth=search_depth,
                timeout=timeout,
            )
            for q in queries
        ]
        # 等待任务完成并收集结果
        for future in as_completed(futures):
            try:
                collected.extend(future.result())
            except Exception:
                # 容错处理：单个查询失败或超时不应导致整个深度搜索崩溃
                continue

    # 按照 URL 进行去重处理
    by_url: dict[str, SearchResult] = {}
    for result in collected:
        key = _normalize_url(result.url) # 去掉参数后的干净 URL 作为 key
        current = by_url.get(key)
        # 如果是新 URL，或者相同 URL 但当前这条得分更高，则记录/替换
        if current is None or result.score > current.score:
            by_url[key] = result

    deduped = list(by_url.values())
    
    # 核心排序逻辑：优先看“信息源质量打分”，同分则看“搜索引擎相关性得分”，再看“正文长度”
    deduped.sort(
        key=lambda r: (_source_quality(r.source), r.score, len(r.content)),
        reverse=True,
    )
    
    # 截断返回所需的最大来源数量
    return deduped[: max(1, min(max_sources, MAX_TOTAL_RESULTS))]


def _format_results(original_query: str, queries: list[str], results: list[SearchResult]) -> str:
    """将搜索结果格式化为易于大模型阅读的 Markdown 格式的“证据包”。"""
    if not results:
        return (
            f"deep_search 没有找到可用来源：{original_query!r}。\n"
            "可尝试放宽 time_range、减少限定词，或拆成更具体的研究问题。"
        )

    lines: list[str] = [
        f"# Deep Search Evidence Pack: {original_query}",
        "",
        "## Search Plan", # 展示搜索计划（拆解了哪些词）
    ]
    for i, q in enumerate(queries, 1):
        lines.append(f"{i}. {q}")

    lines.extend(["", "## Sources"])
    # 逐个列出找到的信息源详情
    for i, result in enumerate(results, 1):
        lines.extend(
            [
                f"### {i}. {result.title}",
                f"- Source: {result.source}",
                f"- URL: {result.url}",
                f"- Matched query: {result.query}",
                f"- Tavily score: {result.score:.3f}",
                "",
                result.content or "(无正文摘要，只有搜索结果元数据)",
                "",
            ]
        )

    # 在末尾附上给大模型 Prompt 的硬性要求（System Prompt 注入）
    lines.extend(
        [
            "## How To Use",
            "- 先用上面的来源做交叉验证，再给结论。",
            "- 关键数字必须引用对应 Source/URL。",
            "- 若来源互相冲突，优先官方/一手来源，并明确冲突点。",
        ]
    )
    return "\n".join(lines).strip()


@mcp.tool()
def deep_search(
    query: str,
    max_queries: int = 5,
    max_sources: int = 8,
    time_range: str | None = None,
    search_depth: str = "advanced",
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """围绕一个投研问题执行多 query 深度检索，返回去重后的证据包。

    适合：
      · 需要从新闻/公告/研报/行业材料里交叉验证一个主题
      · 写报告前收集来源
      · 用户明确要求"深度搜索 / deepsearch / 多找几篇来源"

    不适合：
      · 已经有明确 URL，只需读正文
      · 简单行情/财务事实查询（用专用数据工具更快）

    参数：
      query: 研究问题
      max_queries: 自动扩展出的检索 query 数，默认 5
      max_sources: 最终返回来源数，默认 8
      time_range: Tavily 时间范围，可选 day/week/month/year
      search_depth: basic 或 advanced
      timeout_seconds: 每个搜索请求超时，默认 12 秒
    """
    # 1. 基础校验
    q = query.strip()
    if not q:
        return "deep_search 需要非空 query。"
    if not settings.tavily_api_key:
        return "⚠ deep_search 不可用：未配置 TAVILY_API_KEY。"

    # 2. 规范化超时时间 (限制在 3 到 30 秒之间)
    bounded_timeout = max(3, min(timeout_seconds, 30))
    
    # 3. 扩展查询词（最多生成 8 个）
    queries = _build_queries(q, max(1, min(max_queries, 8)))
    
    # 4. 执行并发搜索并去重
    results = _search_many(
        queries,
        max_sources=max_sources,
        time_range=time_range,
        search_depth=search_depth,
        timeout=bounded_timeout,
    )
    
    # 5. 格式化并返回最终结果
    return _format_results(q, queries, results)