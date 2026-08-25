---
name: web-scraper
description: >
  Trigger this skill when you need to understand, extend, or debug the
  Universal Web Scraper and Financial Portal (Moneycontrol) Scraping layer.
  Covers: how scrape_url handles arbitrary URLs, how scrape_moneycontrol extracts
  deep Indian financial metrics, Playwright headless browser fallback, caching,
  and anti-bottleneck parallelization strategies.
tags:
  - scraper
  - moneycontrol
  - web-scraping
  - playwright
  - financial-data
---

# Skill: Universal Web & Financial Portal Scraper

## When to activate this skill
Load this skill when:
- You need to scrape, extract, or parse financial portal data (Moneycontrol, Screener.in, Trendlyne, NSE/BSE).
- You need to scrape arbitrary web URLs, corporate announcements, filings, or press releases.
- You need to extract structured tables or clean markdown from messy HTML.
- You need to configure or debug the Playwright headless browser fallback.
- You want to understand how caching and anti-bottleneck concurrency safeguards work.

---

## Architecture Overview

```
                        ┌──────────────────────────────────────────────┐
                        │              DSH Agent Runtime               │
                        └──────────────────────┬───────────────────────┘
                                               │ Dispatches MCP Call
                                               ▼
                        ┌──────────────────────────────────────────────┐
                        │            harness/mcp_server.py             │
                        └──────────────────────┬───────────────────────┘
                                               │ Calls
                                               ▼
                        ┌──────────────────────────────────────────────┐
                        │           tools/scraper_tools.py             │
                        ├──────────────────────────────────────────────┤
                        │ 1. Disk/Memory Cache (cache/scraper/)        │
                        │ 2. Autosuggest & URL Resolvers               │
                        │ 3. Fast-Path HTTP/2 Engine (httpx)           │
                        │ 4. Playwright Headless Browser Fallback      │
                        │ 5. Table & Markdown Structured Parser (bs4)  │
                        └──────────────────────────────────────────────┘
```

---

## Core Tool Functions

### 1. `scrape_url(url, selector, extract_mode, use_browser, max_length, no_cache)`
Universal web scraper capable of fetching and parsing **ANY** website or API endpoint.

- **Fast-Path**: Uses `httpx` with desktop browser emulation headers.
- **Dynamic JS Fallback**: If `use_browser=True` or if the HTTP request returns `403`/`429`/`503`, automatically launches headless Chromium via Playwright to bypass client-side rendering bottlenecks.
- **Extraction Modes**:
  - `auto`: Extracts page title, meta description, structured tables (as JSON lists of records), and clean markdown prose.
  - `tables`: Extracts all HTML tables into structured JSON.
  - `text`: Extracts readable article/filing text in Markdown.
  - `json`: Returns parsed JSON directly if the target URL is a REST API endpoint.

### 2. `scrape_moneycontrol(query_or_ticker, section, use_browser, no_cache)`
Specialized scraper for Moneycontrol.com.

- **Auto-Resolution**: Queries Moneycontrol Solr autosuggest API (`autosuggestion_solr.php`) to find the exact canonical quote URL from simple ticker symbols (`TCS.NS`, `RELIANCE`, `HDFCBANK`) or company names (`"Tata Motors"`).
- **High-Value Metrics Extracted**:
  - `20d_avg_delivery_pct`: 20-Day Average Delivery % (institutional conviction indicator).
  - `vwap`: Volume-Weighted Average Price.
  - `beta`: Volatility relative to Nifty 50.
  - `52_week_high` / `52_week_low`: 52-Week trading range.
  - `all_time_high` / `all_time_low`: Lifetime price boundaries.
  - `book_value`: Book Value per share.
  - `market_cap_cr`: Market Capitalization in INR Crores.
  - `face_value`: Share face value.

---

## Caching & Concurrency Safeguards

1. **TTL Caching (`cache/scraper/{hash}.json`)**:
   - Every scrape query is hashed (SHA-256) and cached with a 1-hour TTL.
   - Repeated queries from DSH or subagents resolve in **0ms** from disk without re-hitting external websites.
2. **Transient Error Retries**:
   - Decorated with `@retry_on_transient_error(max_attempts=2)` with exponential backoff on network blips.
3. **Noisy Element Stripping**:
   - Automatically decomposes `script`, `style`, `nav`, `header`, `footer`, `iframe`, `svg`, and `form` elements to avoid context window bloating.

---

## How to Extend for New Financial Portals (e.g. Screener.in, Trendlyne)

To add another specialized portal scraper:
1. Implement a resolver function in `tools/scraper_tools.py` (e.g. `scrape_screener(ticker)`).
2. Use `_fetch_http` or `_fetch_browser` to retrieve the page.
3. Use `_extract_tables_from_soup` or `_clean_html_to_markdown` for structured parsing.
4. Register the new tool in `harness/mcp_server.py` and `cordis.yml`.
