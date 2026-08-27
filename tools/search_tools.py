"""
Tavily web search wrapper — exposed to the master orchestrator and research loops.

Returns a list of dicts (title/url/content/score) rather than a Tavily-specific object.
"""
from __future__ import annotations

import logging

from tavily import TavilyClient

from config import settings
from utils.retry import retry_on_transient_error
from tools.rate_limits import tavily_budget

logger = logging.getLogger(__name__)

_client = TavilyClient(api_key=settings.tavily_api_key)


def search_web_news(
    query: str,
    ticker: str = "",
    depth: str = "basic",
    max_results: int = 5,
) -> list[dict]:
    """
    Search the live web for news/sentiment relevant to `query`.
    `depth` may be 'basic' (1 credit) or 'advanced' (2 credits).
    """
    if not tavily_budget.increment():
        logger.warning("Tavily search budget exhausted. No additional queries permitted.")
        return []

    return _search_web_news_core(query, ticker, depth, max_results)

@retry_on_transient_error(max_attempts=3)
def _search_web_news_core(
    query: str,
    ticker: str = "",
    depth: str = "basic",
    max_results: int = 5,
) -> list[dict]:

    max_results = max(1, min(max_results, 10))
    search_depth = "advanced" if depth.lower() == "advanced" else "basic"

    full_query = query.strip()
    if ticker and ticker.upper() not in full_query.upper():
        full_query = f"{ticker} {full_query}"

    response = _client.search(
        query=full_query,
        search_depth=search_depth,
        topic="finance",
        max_results=max_results,
        include_answer=False,
    )

    results = []
    for item in response.get("results", []):
        results.append(
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "content": item.get("content", ""),
                "score": item.get("score"),
            }
        )

    if not results:
        logger.warning("Tavily returned no results for query: %r", full_query)

    return results


def investigate_financial_anomaly(
    company_name: str,
    ticker: str,
    anomaly_type: str,
    metric_impacted: str = "",
    observed_value: str = "",
    prior_value: str = "",
    query_hint: str = "",
) -> dict[str, Any]:
    """
    Contextual deep-dive anomaly hunter ("The Why Loop").
    When a quantitative anomaly is identified (e.g. sharp QoQ profit drop, margin compression, debt spike),
    issues a targeted search to locate regulatory filings and earnings report explanations.
    """
    search_terms = f"{company_name or ticker} {anomaly_type} {metric_impacted} {query_hint} earnings results reason explanation charge one-off"
    results = search_web_news(query=search_terms, ticker=ticker, depth="basic", max_results=3)

    findings = []
    for r in results:
        findings.append({
            "anomaly_type": anomaly_type,
            "metric_impacted": metric_impacted or anomaly_type,
            "observed_value": observed_value,
            "prior_or_expected_value": prior_value,
            "driver_explanation": r.get("content", "")[:350],
            "source_url": r.get("url", ""),
            "severity": "high" if any(w in anomaly_type.lower() for w in ("drop", "plunge", "loss", "spike", "fraud", "penalty", "impairment")) else "medium",
        })

    return {
        "ticker": ticker,
        "anomaly_type": anomaly_type,
        "metric_impacted": metric_impacted,
        "investigation_status": "completed" if findings else "inconclusive",
        "findings_count": len(findings),
        "findings": findings,
        "summary": f"Identified {len(findings)} potential qualitative explanations for {anomaly_type}." if findings else "No specific cited driver found in public news.",
    }
