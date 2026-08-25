"""
Universal Resilient Web & Financial Portal Scraper.

Provides two core capabilities:
1. scrape_url: Universal web scraper for ANY website. Extracts clean markdown,
   structured tables, metadata, and handles fast-path HTTP/2 with Playwright headless fallback.
2. scrape_moneycontrol: Specialized financial portal scraper for Moneycontrol.com.
   Resolves tickers/companies via autosuggest, extracts key valuation multiples, 52W range,
   VWAP, Beta, 20D delivery %, and market depth tables.

Features built-in disk caching (cache/scraper/) with TTL to avoid redundant requests.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup
import httpx

from config import settings
from utils.retry import retry_on_transient_error

logger = logging.getLogger("scraper_tools")

_SCRAPER_CACHE_DIR = settings.cache_dir / "scraper"
_SCRAPER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_DEFAULT_CACHE_TTL = 3600  # 1 hour

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


# ---------------------------------------------------------------------------
# Caching helpers
# ---------------------------------------------------------------------------

def _get_cache_path(key: str) -> Path:
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return _SCRAPER_CACHE_DIR / f"{h}.json"


def _read_cache(key: str, ttl_seconds: int = _DEFAULT_CACHE_TTL) -> Optional[dict[str, Any]]:
    path = _get_cache_path(key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cached_time = data.get("_cached_at", 0)
        if time.time() - cached_time < ttl_seconds:
            return data.get("payload")
    except Exception as exc:
        logger.debug("Cache read error for %s: %s", key, exc)
    return None


def _write_cache(key: str, payload: dict[str, Any]) -> None:
    path = _get_cache_path(key)
    try:
        record = {"_cached_at": time.time(), "payload": payload}
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.debug("Cache write error for %s: %s", key, exc)


# ---------------------------------------------------------------------------
# HTML Cleaning & Table Extraction Engine
# ---------------------------------------------------------------------------

def _extract_tables_from_soup(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Extract all HTML tables into structured JSON lists of records."""
    tables_data = []
    for idx, table in enumerate(soup.find_all("table")):
        headers: list[str] = []
        thead = table.find("thead")
        if thead:
            headers = [th.get_text(separator=" ", strip=True) for th in thead.find_all(["th", "td"]) if th.get_text(strip=True)]

        tbody = table.find("tbody") or table
        rows_data = []
        for tr in tbody.find_all("tr"):
            if tr.find_parent("thead"):
                continue
            th_cells = tr.find_all("th")
            td_cells = tr.find_all("td")
            if th_cells and not td_cells and not headers:
                headers = [th.get_text(separator=" ", strip=True) for th in th_cells]
                continue
            cells = [c.get_text(separator=" ", strip=True) for c in tr.find_all(["th", "td"])]
            if not cells or all(not c for c in cells):
                continue
            if headers and cells == headers:
                continue
            if headers and len(cells) == len(headers):
                rows_data.append(dict(zip(headers, cells)))
            else:
                rows_data.append({"cells": cells})

        if rows_data:
            table_id = table.get("id") or table.get("class") or f"table_{idx}"
            tables_data.append({
                "table_identifier": str(table_id),
                "headers": headers,
                "rows_count": len(rows_data),
                "rows": rows_data[:50],  # cap per table
            })
    return tables_data


def _clean_html_to_markdown(html_content: str, max_chars: int = 10000) -> dict[str, Any]:
    """Parse raw HTML into clean title, description, tables, and readable markdown text."""
    soup = BeautifulSoup(html_content, "lxml" if "lxml" in BeautifulSoup.__module__ else "html.parser")

    # Extract metadata
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    elif soup.find("h1"):
        title = soup.find("h1").get_text(strip=True)

    description = ""
    meta_desc = soup.find("meta", attrs={"name": re.compile(r"description", re.I)}) or \
                soup.find("meta", attrs={"property": "og:description"})
    if meta_desc and meta_desc.get("content"):
        description = meta_desc["content"].strip()

    # Extract structured tables before removing table elements
    tables = _extract_tables_from_soup(soup)

    # Decompose script, style, header, footer, nav, ads
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "svg", "iframe", "form", "button"]):
        tag.decompose()

    # Get body or main container
    main_elem = soup.find("main") or soup.find("article") or soup.find("body") or soup

    # Convert paragraphs and headings to clean text
    lines = []
    for elem in main_elem.find_all(["h1", "h2", "h3", "h4", "p", "li", "tr"]):
        text = elem.get_text(separator=" ", strip=True)
        if not text:
            continue
        tag_name = elem.name
        if tag_name == "h1":
            lines.append(f"\n# {text}\n")
        elif tag_name == "h2":
            lines.append(f"\n## {text}\n")
        elif tag_name == "h3":
            lines.append(f"\n### {text}\n")
        elif tag_name == "li":
            lines.append(f"- {text}")
        else:
            lines.append(text)

    full_text = "\n".join(lines)
    full_text = re.sub(r"\n{3,}", "\n\n", full_text).strip()

    if len(full_text) > max_chars:
        full_text = full_text[:max_chars] + f"\n\n... [Content truncated to {max_chars} characters]"

    return {
        "title": title,
        "description": description,
        "tables_count": len(tables),
        "tables": tables,
        "text": full_text,
    }


# ---------------------------------------------------------------------------
# Universal Scraper Implementation
# ---------------------------------------------------------------------------

@retry_on_transient_error(max_attempts=2)
def _fetch_http(url: str, timeout: int = 15) -> httpx.Response:
    with httpx.Client(headers=_DEFAULT_HEADERS, follow_redirects=True, timeout=timeout) as client:
        return client.get(url)


def _fetch_browser(url: str, wait_selector: Optional[str] = None, timeout: int = 20) -> str:
    """Headless Playwright browser fetcher for dynamic / JavaScript-heavy websites."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=_DEFAULT_HEADERS["User-Agent"],
                viewport={"width": 1280, "height": 800},
            )
            page = context.new_page()
            page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=5000)
                except Exception:
                    pass
            else:
                time.sleep(1.0)  # Brief wait for initial hydration
            content = page.content()
            browser.close()
            return content
    except Exception as exc:
        logger.warning("Playwright fetch failed for %s: %s — falling back to HTTP", url, exc)
        resp = _fetch_http(url)
        return resp.text


def _sanitize_metric_value(val: Any) -> Any:
    """Clean and standardize scraped table values."""
    if not isinstance(val, str) or not val:
        return val
    cleaned = re.sub(r"\s+", " ", val).strip()
    if cleaned in ("--", "-", "N/A", "null"):
        return None
    # If multiple space-separated numbers e.g. '1.82 1.77' (NSE BSE Betas)
    parts = cleaned.split(" ")
    if len(parts) >= 2:
        if re.match(r"^[-+]?\d*\.?\d+(?:,\d+)*$", parts[0]):
            return parts[0]
    return cleaned


def _filter_key_metrics(raw_metrics: dict[str, Any], requested_fields: list[str]) -> dict[str, Any]:
    """Fuzzy match and extract requested fields from key-value metrics."""
    filtered: dict[str, Any] = {}
    for req in requested_fields:
        req_clean = re.sub(r"[\s_\-\(\)%]+", "", req.lower())
        matched = False
        for k, v in raw_metrics.items():
            k_clean = re.sub(r"[\s_\-\(\)%]+", "", k.lower())
            if req_clean in k_clean or k_clean in req_clean:
                filtered[req] = _sanitize_metric_value(v)
                matched = True
                break
        if not matched:
            filtered[req] = None
    return filtered


def scrape_url(
    url: str,
    fields: Optional[list[str]] = None,
    selector: Optional[str] = None,
    extract_mode: str = "auto",
    use_browser: bool = False,
    max_length: int = 8000,
    no_cache: bool = False,
) -> dict[str, Any]:
    """
    Universal web scraper capable of fetching and parsing ANY website or API endpoint.

    Args:
        url: Target web URL (HTTP or HTTPS).
        fields: Optional list of specific field names or metrics to extract (e.g. ['Revenue', 'Operating Profit', 'Margin']).
        selector: Optional CSS selector to focus extraction on a specific container.
        extract_mode: 'auto' (clean text + tables), 'tables' (tables only),
                      'text' (markdown text only), 'json' (raw JSON API).
        use_browser: If True, uses headless Chromium to render client-side JavaScript.
        max_length: Maximum character length for text extraction.
        no_cache: Force live fetch, bypassing local cache.

    Returns:
        Structured dictionary with title, text content, tables, status, and URL metadata.
    """
    if not url or not url.startswith(("http://", "https://")):
        return {"status": "error", "error": f"Invalid URL: {url!r}"}

    fields_str = ",".join(sorted(fields)) if fields else "all"
    cache_key = f"scrape_url:{url}:{extract_mode}:{use_browser}:{selector}:{fields_str}"
    if not no_cache:
        cached = _read_cache(cache_key)
        if cached:
            cached["cached"] = True
            return cached

    start_time = time.time()
    try:
        # 1. Fetch raw content
        raw_html = ""
        is_json = False
        json_data = None

        if use_browser:
            raw_html = _fetch_browser(url, wait_selector=selector)
        else:
            resp = _fetch_http(url)
            if resp.status_code >= 400:
                # If blocked with 403 / 429, attempt browser fallback
                if resp.status_code in (403, 429, 503):
                    logger.info("HTTP %d received for %s — attempting Playwright browser fallback", resp.status_code, url)
                    raw_html = _fetch_browser(url, wait_selector=selector)
                else:
                    return {
                        "status": "error",
                        "status_code": resp.status_code,
                        "url": url,
                        "error": f"HTTP {resp.status_code}: {resp.reason_phrase}",
                    }
            else:
                content_type = resp.headers.get("content-type", "")
                if "application/json" in content_type or extract_mode == "json":
                    try:
                        json_data = resp.json()
                        is_json = True
                    except Exception:
                        raw_html = resp.text
                else:
                    raw_html = resp.text

        # 2. Handle JSON response
        if is_json:
            result = {
                "status": "ok",
                "url": url,
                "domain": urlparse(url).netloc,
                "content_type": "application/json",
                "json_data": json_data,
                "fetch_time_seconds": round(time.time() - start_time, 2),
            }
            _write_cache(cache_key, result)
            return result

        # 3. Handle HTML extraction
        soup = BeautifulSoup(raw_html, "lxml" if "lxml" in BeautifulSoup.__module__ else "html.parser")
        if selector:
            target_elem = soup.select_one(selector)
            if target_elem:
                raw_html = str(target_elem)

        parsed = _clean_html_to_markdown(raw_html, max_chars=max_length)

        # 4. Extract targeted fields if requested
        extracted_fields_data: dict[str, Any] = {}
        if fields:
            # Flatten table key-value pairs
            flat_kv: dict[str, Any] = {}
            for tbl in parsed.get("tables", []):
                for row in tbl.get("rows", []):
                    if len(row) == 2:
                        k, v = list(row.keys())[0], list(row.values())[0]
                        flat_kv[str(k)] = str(v)
            extracted_fields_data = _filter_key_metrics(flat_kv, fields)

        result = {
            "status": "ok",
            "url": url,
            "domain": urlparse(url).netloc,
            "title": parsed["title"],
            "description": parsed["description"],
            "requested_fields": extracted_fields_data if fields else None,
            "text": parsed["text"] if (not fields and extract_mode in ("auto", "text")) else "",
            "tables_count": parsed["tables_count"],
            "tables": parsed["tables"] if extract_mode in ("auto", "tables") else [],
            "fetch_time_seconds": round(time.time() - start_time, 2),
            "rendered_with_browser": use_browser,
        }

        _write_cache(cache_key, result)
        return result

    except Exception as exc:
        logger.exception("scrape_url failed for %s: %s", url, exc)
        return {
            "status": "error",
            "url": url,
            "error": str(exc),
            "fetch_time_seconds": round(time.time() - start_time, 2),
        }


# ---------------------------------------------------------------------------
# Specialized Moneycontrol Scraper
# ---------------------------------------------------------------------------

def _score_solr_candidate(query: str, item: dict[str, Any]) -> float:
    """Score a Solr autosuggest candidate based on exact name match, word overlap, and ticker codes."""
    import difflib
    raw_name = (item.get("name") or "").lower()
    disp_name = re.sub(r"<.*?>|&nbsp;", " ", (item.get("pdt_dis_nm") or "")).lower()
    sc_id = (item.get("sc_id") or "").lower()
    q = query.lower().strip()

    # 1. Exact ticker code / sc_id match
    if sc_id == q or f", {q}," in disp_name or f" {q} " in disp_name:
        return 3.0

    # 2. Token overlap
    q_words = [w for w in re.split(r"[\s,\.\-_]+", q) if w]
    name_words = [w for w in re.split(r"[\s,\.\-_]+", raw_name) if w]
    disp_words = [w for w in re.split(r"[\s,\.\-_]+", disp_name) if w]
    all_target_words = set(name_words + disp_words)

    overlap_count = sum(1 for w in q_words if w in all_target_words)
    token_score = overlap_count / max(len(q_words), 1)

    # 3. Prefix match
    prefix_score = 1.0 if raw_name.startswith(q) or disp_name.startswith(q) else 0.0

    # 4. String similarity ratio
    ratio = difflib.SequenceMatcher(None, q, raw_name).ratio()

    # Penalize if first query word is completely missing in target (e.g. searching "tata motors" vs "eicher motors")
    if q_words and q_words[0] not in all_target_words:
        token_score *= 0.05

    return token_score * 2.0 + prefix_score * 0.8 + ratio * 0.4


def _resolve_moneycontrol_url(query: str) -> Optional[dict[str, Any]]:
    """Query Moneycontrol's Solr autosuggest API to find exact quote URL and stock metadata."""
    clean_q = re.sub(r"\.(NS|BO|BSE|NSE)$", "", query.strip(), flags=re.I)
    suggest_url = f"https://www.moneycontrol.com/mccode/common/autosuggestion_solr.php?query={clean_q}&type=1&format=json"

    try:
        resp = _fetch_http(suggest_url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and data:
                # Rank candidates by relevance to query
                ranked = sorted(data, key=lambda x: _score_solr_candidate(clean_q, x), reverse=True)
                best = ranked[0]
                return {
                    "company_name": best.get("name") or best.get("pdt_dis_nm"),
                    "quote_url": best.get("link_src"),
                    "stock_id": best.get("sc_id"),
                    "display_name": best.get("pdt_dis_nm"),
                }
    except Exception as exc:
        logger.warning("Moneycontrol autosuggest failed for %s: %s", query, exc)
    return None


def scrape_moneycontrol(
    query_or_ticker: str,
    fields: Optional[list[str]] = None,
    section: str = "overview",
    use_browser: bool = False,
    no_cache: bool = False,
) -> dict[str, Any]:
    """
    Dedicated Moneycontrol.com financial portal scraper.

    Resolves Indian stocks to their Moneycontrol overview page and extracts key metrics
    not easily available on raw yfinance (e.g. 20D Avg Delivery %, VWAP, Beta, Standalone vs Consolidated P/E).

    Args:
        query_or_ticker: Company ticker (e.g. 'TCS.NS', 'RELIANCE', 'HDFCBANK') or company name ('Tata Motors').
        fields: Optional list of specific fields requested (e.g. ['beta', 'delivery', 'vwap', '52_week_high']).
        section: Financial section to extract ('overview', 'financials', 'ratios', 'peers').
        use_browser: Use Playwright headless browser for dynamic charts.
        no_cache: Force fresh scrape.

    Returns:
        Structured dictionary containing parsed metrics, ratios, market technicals, and clean markdown.
    """
    target_url = query_or_ticker
    meta_info: dict[str, Any] = {}

    if not query_or_ticker.startswith(("http://", "https://")):
        resolved = _resolve_moneycontrol_url(query_or_ticker)
        if not resolved or not resolved.get("quote_url"):
            return {
                "status": "error",
                "query": query_or_ticker,
                "error": f"Could not resolve Moneycontrol URL for '{query_or_ticker}'",
            }
        target_url = resolved["quote_url"]
        meta_info = resolved

    fields_str = ",".join(sorted(fields)) if fields else "all"
    cache_key = f"scrape_moneycontrol:{target_url}:{section}:{use_browser}:{fields_str}"
    if not no_cache:
        cached = _read_cache(cache_key)
        if cached:
            cached["cached"] = True
            return cached

    # Scrape the page
    scrape_res = scrape_url(target_url, use_browser=use_browser, extract_mode="auto", max_length=12000, no_cache=no_cache)
    if scrape_res.get("status") != "ok":
        return scrape_res

    # Extract key-value financial pairs from page tables
    key_metrics: dict[str, Any] = {}
    for table_info in scrape_res.get("tables", []):
        for row in table_info.get("rows", []):
            cells = row.get("cells", [])
            if len(cells) == 2 and cells[0] and cells[1] and cells[1] != "--":
                key_metrics[cells[0]] = cells[1]

    # Normalized extraction of high-value Indian market metrics
    overview_metrics = {
        "company_name": meta_info.get("company_name") or scrape_res.get("title", ""),
        "quote_url": target_url,
        "market_cap_cr": key_metrics.get("Mkt Cap (Rs. Cr.)") or key_metrics.get("Market Cap (Rs Cr.)"),
        "pe_ratio": key_metrics.get("P/E") or key_metrics.get("PE"),
        "sector_pe": key_metrics.get("Sector P/E"),
        "pb_ratio": key_metrics.get("P/B"),
        "book_value": key_metrics.get("Book Value Per Share") or key_metrics.get("Book Value"),
        "dividend_yield": key_metrics.get("Dividend Yield (%)"),
        "beta": key_metrics.get("Beta"),
        "vwap": key_metrics.get("VWAP"),
        "52_week_high": key_metrics.get("52 Week High"),
        "52_week_low": key_metrics.get("52 Week Low"),
        "all_time_high": key_metrics.get("All Time High"),
        "all_time_low": key_metrics.get("All Time Low"),
        "20d_avg_volume": key_metrics.get("20D Avg Volume"),
        "20d_avg_delivery_pct": key_metrics.get("20D Avg Delivery(%)"),
        "face_value": key_metrics.get("Face Value"),
    }

    # Selective field filtering if agent requested specific metrics
    filtered_fields: dict[str, Any] = {}
    if fields:
        combined_lookup = {**key_metrics, **overview_metrics}
        filtered_fields = _filter_key_metrics(combined_lookup, fields)

    result = {
        "status": "ok",
        "portal": "Moneycontrol",
        "target": query_or_ticker,
        "resolved_url": target_url,
        "requested_fields": filtered_fields if fields else None,
        "overview_metrics": overview_metrics if not fields else {k: overview_metrics.get(k) for k in fields if k in overview_metrics},
        "raw_key_metrics_count": len(key_metrics),
        "raw_key_metrics": key_metrics if not fields else None,
        "page_title": scrape_res.get("title"),
        "page_summary": scrape_res.get("description"),
        "tables_count": scrape_res.get("tables_count", 0),
        "text_content_preview": scrape_res.get("text", "")[:2000] if not fields else "",
    }

    _write_cache(cache_key, result)
    return result
