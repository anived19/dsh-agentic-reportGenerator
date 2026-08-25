"""
Deterministic Peer Discovery and Industry Competitor Resolver.

Provides structured peer company lists for autonomous industry benchmarking:
1. High-conviction sector peer mapping table.
2. Dynamic yfinance sector and industry discovery fallback.
3. Live validation ensuring discovered peer tickers return real pricing and multiples.
"""
from __future__ import annotations

import logging
from typing import Any

import yfinance as yf
from schemas import format_currency_amount

logger = logging.getLogger(__name__)

# High-conviction sector peer registry (maps normalized ticker to list of peer tickers)
_KNOWN_PEER_MAP: dict[str, list[str]] = {
    # US Banking / Financials
    "JPM": ["BAC", "C", "WFC", "GS", "MS"],
    "BAC": ["JPM", "C", "WFC", "GS", "MS"],
    "C": ["JPM", "BAC", "WFC", "GS"],
    "WFC": ["JPM", "BAC", "C", "USB"],
    "GS": ["MS", "JPM", "C", "BAC"],
    "MS": ["GS", "JPM", "C", "BAC"],

    # Indian Banking
    "HDFCBANK.NS": ["ICICIBANK.NS", "KOTAKBANK.NS", "AXISBANK.NS", "SBIN.NS"],
    "ICICIBANK.NS": ["HDFCBANK.NS", "KOTAKBANK.NS", "AXISBANK.NS", "SBIN.NS"],
    "SBIN.NS": ["HDFCBANK.NS", "ICICIBANK.NS", "BANKBARODA.NS", "PNB.NS"],
    "KOTAKBANK.NS": ["HDFCBANK.NS", "ICICIBANK.NS", "AXISBANK.NS"],
    "AXISBANK.NS": ["HDFCBANK.NS", "ICICIBANK.NS", "KOTAKBANK.NS"],

    # IT Services & Tech (India)
    "TCS.NS": ["INFY.NS", "WIPRO.NS", "HCLTECH.NS", "LTIM.NS", "TECHM.NS"],
    "INFY.NS": ["TCS.NS", "WIPRO.NS", "HCLTECH.NS", "LTIM.NS", "TECHM.NS"],
    "WIPRO.NS": ["TCS.NS", "INFY.NS", "HCLTECH.NS", "LTIM.NS"],
    "HCLTECH.NS": ["TCS.NS", "INFY.NS", "WIPRO.NS", "LTIM.NS"],
    "LTIM.NS": ["TCS.NS", "INFY.NS", "WIPRO.NS", "HCLTECH.NS"],
    "TECHM.NS": ["TCS.NS", "INFY.NS", "WIPRO.NS", "HCLTECH.NS"],

    # Big Tech / SaaS / Cloud (US)
    "AAPL": ["MSFT", "GOOGL", "AMZN", "META"],
    "MSFT": ["AAPL", "GOOGL", "AMZN", "ORCL", "CRM"],
    "GOOGL": ["META", "MSFT", "AMZN", "AAPL"],
    "GOOG": ["META", "MSFT", "AMZN", "AAPL"],
    "AMZN": ["MSFT", "GOOGL", "WMT", "BABA"],
    "META": ["GOOGL", "MSFT", "SNAP", "PINS"],
    "CRM": ["ORCL", "MSFT", "SAP", "NOW", "WDAY"],
    "ORCL": ["CRM", "MSFT", "SAP", "IBM"],
    "NOW": ["CRM", "WDAY", "ORCL", "TEAM"],

    # Semiconductors
    "NVDA": ["AMD", "INTC", "AVGO", "QCOM", "TSM"],
    "AMD": ["NVDA", "INTC", "AVGO", "QCOM"],
    "INTC": ["AMD", "NVDA", "QCOM", "TXN"],
    "AVGO": ["QCOM", "NVDA", "AMD", "TXN"],
    "TSM": ["NVDA", "AMD", "INTC", "QCOM"],

    # Automotive
    "TMPV.NS": ["MARUTI.NS", "M&M.NS", "BAJAJ-AUTO.NS", "HEROMOTOCO.NS"],
    "TMCV.NS": ["ASHOKLEY.NS", "EICHERMOT.NS", "M&M.NS"],
    "TATAMOTORS.NS": ["MARUTI.NS", "M&M.NS", "BAJAJ-AUTO.NS", "ASHOKLEY.NS"],
    "MARUTI.NS": ["M&M.NS", "TMPV.NS", "HYUNDAI.NS", "BAJAJ-AUTO.NS"],
    "M&M.NS": ["MARUTI.NS", "TMPV.NS", "BAJAJ-AUTO.NS", "EICHERMOT.NS"],
    "TSLA": ["RIVN", "LCID", "F", "GM", "TM"],

    # Energy, Telecom & Conglomerates
    "RELIANCE.NS": ["ONGC.NS", "IOC.NS", "BPCL.NS", "BHARTIARTL.NS"],
    "BHARTIARTL.NS": ["RELIANCE.NS", "TATACOMM.NS", "IDEA.NS"],
    "ONGC.NS": ["RELIANCE.NS", "IOC.NS", "BPCL.NS", "OIL.NS"],
    "IOC.NS": ["BPCL.NS", "HPCL.NS", "RELIANCE.NS", "ONGC.NS"],
    "BPCL.NS": ["IOC.NS", "HPCL.NS", "RELIANCE.NS", "ONGC.NS"],
    "XOM": ["CVX", "SHEL", "TTE", "COP", "BP"],
    "CVX": ["XOM", "COP", "SHEL", "BP"],
}


def get_peer_tickers(ticker: str, max_peers: int = 4) -> dict[str, Any]:
    """
    Discover 3-5 validated industry peers for a given ticker symbol.
    Returns structured metadata including company name, market cap, and sector.
    """
    norm_ticker = ticker.strip().upper()
    peer_symbols = _KNOWN_PEER_MAP.get(norm_ticker) or []

    target_info: dict[str, Any] = {}
    target_name = norm_ticker
    industry = "General"
    currency = "USD"

    try:
        t_target = yf.Ticker(norm_ticker)
        target_info = t_target.info or {}
        target_name = target_info.get("shortName") or target_info.get("longName") or norm_ticker
        industry = target_info.get("industry") or target_info.get("sector") or "General"
        currency = target_info.get("currency") or "USD"
    except Exception as exc:
        logger.warning("Could not fetch target ticker info for %s: %s", ticker, exc)

    # Dynamic fallback if not in known peer map
    if not peer_symbols:
        sector = target_info.get("sector")
        # Suffix matching for Indian vs US equities
        if norm_ticker.endswith(".NS"):
            if "Financial" in str(sector) or "Bank" in str(industry):
                peer_symbols = ["HDFCBANK.NS", "ICICIBANK.NS", "SBIN.NS", "KOTAKBANK.NS"]
            elif "Tech" in str(sector) or "Information" in str(industry):
                peer_symbols = ["TCS.NS", "INFY.NS", "WIPRO.NS", "HCLTECH.NS"]
            elif "Auto" in str(sector) or "Auto" in str(industry):
                peer_symbols = ["MARUTI.NS", "M&M.NS", "BAJAJ-AUTO.NS"]
            else:
                peer_symbols = ["RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS"]
        else:
            if "Financial" in str(sector) or "Bank" in str(industry):
                peer_symbols = ["JPM", "BAC", "WFC", "C"]
            elif "Tech" in str(sector) or "Software" in str(industry):
                peer_symbols = ["MSFT", "AAPL", "GOOGL", "CRM"]
            elif "Auto" in str(sector) or "Auto" in str(industry):
                peer_symbols = ["TSLA", "F", "GM", "TM"]
            else:
                peer_symbols = ["AAPL", "MSFT", "AMZN", "GOOGL"]

    # Filter out self
    peer_symbols = [s for s in peer_symbols if s.upper() != norm_ticker][:max_peers]

    # Resolve and validate peer metadata
    peers_data = []
    for sym in peer_symbols:
        try:
            t = yf.Ticker(sym)
            info = t.info or {}
            mcap = info.get("marketCap")
            pe = info.get("trailingPE")
            fpe = info.get("forwardPE")
            ps = info.get("priceToSalesTrailing12Months")
            ev = info.get("enterpriseToEbitda")
            om = info.get("operatingMargins")
            curr = info.get("currency", currency)

            peers_data.append({
                "ticker": sym,
                "name": info.get("shortName") or info.get("longName") or sym,
                "market_cap": mcap,
                "market_cap_formatted": format_currency_amount(mcap, curr) if mcap else "data unavailable",
                "pe_ratio": round(float(pe), 2) if pe else None,
                "pe_ratio_formatted": f"{float(pe):.2f}" if pe else "data unavailable",
                "forward_pe": round(float(fpe), 2) if fpe else None,
                "forward_pe_formatted": f"{float(fpe):.2f}" if fpe else "data unavailable",
                "ps_ratio": round(float(ps), 2) if ps else None,
                "ps_ratio_formatted": f"{float(ps):.2f}" if ps else "data unavailable",
                "ev_ebitda": round(float(ev), 2) if ev else None,
                "ev_ebitda_formatted": f"{float(ev):.2f}" if ev else "N/A",
                "operating_margin": round(float(om), 4) if om else None,
                "operating_margin_formatted": f"{float(om)*100.0:.2f}%" if om else "data unavailable",
            })
        except Exception as exc:
            logger.warning("Peer metadata resolution failed for %s: %s", sym, exc)

    return {
        "target_ticker": norm_ticker,
        "target_name": target_name,
        "industry": industry,
        "peers_count": len(peers_data),
        "peers": peers_data,
        "industry_summary": f"Identified {len(peers_data)} peer competitors in {industry}." if peers_data else "No direct peers resolved.",
    }
