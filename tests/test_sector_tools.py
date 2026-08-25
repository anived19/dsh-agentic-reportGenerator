"""
Unit tests for deterministic sector-specific calculators:
- compute_banking_metrics (Banks & Financial Institutions)
- compute_saas_metrics (SaaS, Cloud & IT Services)
- compute_retail_consumer_metrics (Retail, Manufacturing & Consumer Goods)
"""
from unittest.mock import MagicMock, patch
import pandas as pd
import pytest

from tools.finance_tools import (
    compute_banking_metrics,
    compute_saas_metrics,
    compute_retail_consumer_metrics,
)


@pytest.fixture
def mock_bank_ticker():
    t = MagicMock()
    t.info = {
        "currency": "USD",
        "totalAssets": 4000000000000,
        "totalRevenue": 180000000000,
        "operatingMargins": 0.35,
        "returnOnAssets": 0.0125,
        "bookValue": 110.0,
        "sharesOutstanding": 2800000000,
    }
    return t


@pytest.fixture
def mock_saas_ticker():
    t = MagicMock()
    t.info = {
        "currency": "USD",
        "totalRevenue": 20000000000,
        "revenueGrowth": 0.22,
        "freeCashflow": 4500000000,
        "fullTimeEmployees": 50000,
        "operatingMargins": 0.25,
    }
    return t


@pytest.fixture
def mock_retail_ticker():
    t = MagicMock()
    t.info = {
        "currency": "USD",
        "totalRevenue": 50000000000,
        "totalAssets": 30000000000,
        "grossMargins": 0.28,
    }
    return t


def test_compute_banking_metrics(mock_bank_ticker):
    with patch("yfinance.Ticker", return_value=mock_bank_ticker):
        res = compute_banking_metrics("JPM")
        assert res["ticker"] == "JPM"
        assert res["roa_pct"] == 1.25
        assert res["roa_formatted"] == "1.25%"
        assert res["efficiency_ratio_pct"] == 65.0  # 100 - 35
        assert res["efficiency_ratio_formatted"] == "65.00%"
        assert res["equity_to_assets_pct"] == 7.7  # (110 * 2.8B) / 4T = 308B / 4T = 7.7%
        assert res["equity_to_assets_formatted"] == "7.70%"


def test_compute_saas_metrics(mock_saas_ticker):
    with patch("yfinance.Ticker", return_value=mock_saas_ticker):
        res = compute_saas_metrics("MSFT")
        assert res["ticker"] == "MSFT"
        assert res["fcf_margin_pct"] == 22.5  # 4.5B / 20B = 22.5%
        assert res["rule_of_40_score"] == 44.5  # 22% growth + 22.5% FCF margin = 44.5%
        assert "Outperforming" in res["rule_of_40_status"]
        assert res["revenue_per_employee"] == 400000.0  # 20B / 50,000


def test_compute_retail_consumer_metrics(mock_retail_ticker):
    with patch("yfinance.Ticker", return_value=mock_retail_ticker):
        res = compute_retail_consumer_metrics("WMT")
        assert res["ticker"] == "WMT"
        assert res["asset_turnover"] == 1.67  # 50B / 30B = 1.67x
        assert res["asset_turnover_formatted"] == "1.67x"
