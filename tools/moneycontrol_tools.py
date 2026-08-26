"""
Moneycontrol specialized scraping wrappers for the Finoscale agent.
"""
from typing import Any
from tools.scraper_tools import scrape_moneycontrol

def get_promoter_holding(query_or_ticker: str) -> dict[str, Any]:
    """
    Fetches the promoter holding data for a given company.
    """
    # The 'shareholding' section usually contains promoter holding on moneycontrol.
    return scrape_moneycontrol(
        query_or_ticker=query_or_ticker,
        fields=["promoter", "pledged"],
        section="shareholding"
    )

def get_shareholding_pattern(query_or_ticker: str) -> dict[str, Any]:
    """
    Fetches the broader shareholding pattern (FII, DII, Public, etc.) for a given company.
    """
    return scrape_moneycontrol(
        query_or_ticker=query_or_ticker,
        fields=["fii", "dii", "public", "mutual funds", "institutions"],
        section="shareholding"
    )

def get_board_composition(query_or_ticker: str) -> dict[str, Any]:
    """
    Fetches the board of directors / management team composition.
    """
    return scrape_moneycontrol(
        query_or_ticker=query_or_ticker,
        fields=["chairman", "managing director", "ceo", "independent director", "auditor"],
        section="management"
    )
