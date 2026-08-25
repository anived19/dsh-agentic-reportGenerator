import pandas as pd

from tools.finance_tools import (
    _compute_macd,
    _compute_roce,
    _compute_rsi,
    _volume_trend,
)


def test_compute_rsi_insufficient_data():
    closes = pd.Series([100.0, 101.0, 102.0])
    assert _compute_rsi(closes, period=14) is None


def test_compute_rsi_all_gains():
    # 20 days of strictly increasing prices -> RSI should be 100
    closes = pd.Series([float(100 + i) for i in range(20)])
    rsi = _compute_rsi(closes, period=14)
    assert rsi is not None
    assert rsi == 100.0


def test_compute_rsi_standard_range():
    # Normal alternating price movement
    prices = [100.0, 102.0, 101.0, 103.0, 102.5, 104.0, 103.0, 105.0,
              104.5, 106.0, 105.0, 107.0, 106.5, 108.0, 107.0, 109.0]
    closes = pd.Series(prices)
    rsi = _compute_rsi(closes, period=14)
    assert rsi is not None
    assert 0.0 <= rsi <= 100.0


def test_compute_macd():
    # Insufficient data
    assert _compute_macd(pd.Series([10.0] * 20)) == (None, None, None)

    # 40 days of price data
    closes = pd.Series([100.0 + (i * 0.5) for i in range(45)])
    macd, signal, hist = _compute_macd(closes)
    assert macd is not None
    assert signal is not None
    assert hist is not None
    # For steadily rising prices, MACD > 0
    assert macd > 0


def test_volume_trend():
    # Insufficient length
    assert _volume_trend(pd.Series([1000] * 30)) is None

    # 60 days total: first 40 days avg 1000, last 20 days avg 2000 -> rising
    vols = pd.Series([1000.0] * 40 + [2000.0] * 20)
    assert _volume_trend(vols) == "rising"

    # 60 days total: first 40 days avg 2000, last 20 days avg 500 -> falling
    vols_falling = pd.Series([2000.0] * 40 + [500.0] * 20)
    assert _volume_trend(vols_falling) == "falling"

    # Flat volume
    vols_flat = pd.Series([1000.0] * 60)
    assert _volume_trend(vols_flat) == "flat"


def test_compute_roce():
    # Normal case: EBIT=200, TotalAssets=1000, CurrentLiabilities=200 -> Capital Employed=800 -> ROCE=0.25
    info = {
        "ebit": 200.0,
        "totalAssets": 1000.0,
        "currentLiabilities": 200.0,
    }
    roce = _compute_roce(info)
    assert roce == 0.25

    # Missing fields
    assert _compute_roce({"ebit": 200.0}) is None
    assert _compute_roce({}) is None


def test_build_quarterly_financials_continuity_gap():
    from unittest.mock import MagicMock
    from tools.finance_tools import _build_quarterly_financials

    # 4 columns newest first: 2025-03-31, 2024-12-31, 2024-09-30, 2024-03-31 (6-month gap)
    dates = [
        pd.Timestamp("2025-03-31"),
        pd.Timestamp("2024-12-31"),
        pd.Timestamp("2024-09-30"),
        pd.Timestamp("2024-03-31"),
    ]
    df = pd.DataFrame(
        {
            dates[0]: [1000.0, 200.0],
            dates[1]: [900.0, 180.0],
            dates[2]: [800.0, 150.0],
            dates[3]: [700.0, 120.0],
        },
        index=["Total Revenue", "Net Income"],
    )
    mock_ticker = MagicMock()
    mock_ticker.quarterly_financials = df

    quarters = _build_quarterly_financials(mock_ticker)
    assert len(quarters) == 4
    # The 2024-09-30 quarter (index 2 in newest-first) comes after the 2024-03-31 quarter in chronological order
    assert quarters[2].data_gap_note == "A prior quarter may be missing from source data (yfinance)."
    assert quarters[0].data_gap_note is None
    assert quarters[1].data_gap_note is None


def test_compute_roce_from_financial_statements():
    from unittest.mock import MagicMock
    from tools.finance_tools import _compute_roce

    mock_ticker = MagicMock()
    mock_ticker.income_stmt = pd.DataFrame(
        {"2025-03-31": [600.0]},
        index=["Operating Income"],
    )
    mock_ticker.quarterly_income_stmt = None
    mock_ticker.balance_sheet = pd.DataFrame(
        {"2025-03-31": [2000.0, 500.0]},
        index=["Total Assets", "Current Liabilities"],
    )
    mock_ticker.quarterly_balance_sheet = None

    # Capital Employed = 2000 - 500 = 1500; ROCE = 600 / 1500 = 0.4
    roce = _compute_roce({}, t=mock_ticker)
    assert roce == 0.4


def test_get_fundamentals_analyst_and_targets():
    from unittest.mock import patch, MagicMock
    from tools.finance_tools import get_fundamentals

    mock_ticker = MagicMock()
    mock_ticker.info = {"trailingEps": 120.0, "recommendationKey": "buy"}
    mock_ticker.recommendations = pd.DataFrame(
        [{"strongBuy": 5, "buy": 15, "hold": 8, "sell": 2, "strongSell": 1}]
    )
    mock_ticker.analyst_price_targets = {
        "mean": 2456.122,
        "high": 3480.0,
        "low": 1800.0,
    }
    mock_ticker.income_stmt = None
    mock_ticker.quarterly_income_stmt = None
    mock_ticker.balance_sheet = None
    mock_ticker.quarterly_balance_sheet = None

    with patch("yfinance.Ticker", return_value=mock_ticker):
        res = get_fundamentals("TEST.NS")
        assert res["analyst_buy_count"] == 20
        assert res["analyst_hold_count"] == 8
        assert res["analyst_sell_count"] == 3
        assert res["analyst_target_mean"] == 2456.12
        assert res["analyst_target_mean_formatted"] == "2,456.12"
        assert res["analyst_target_high_formatted"] == "3,480.00"
        assert res["analyst_target_low_formatted"] == "1,800.00"


def test_custom_financial_metric_cagr_success():
    from tools.finance_tools import compute_custom_financial_metric
    
    # 100 to 200 in 3 years -> (200/100)**(1/3) - 1 = ~25.99%
    res = compute_custom_financial_metric(
        expression="cagr(beginning_val, ending_val, 3)",
        context={"beginning_val": 100.0, "ending_val": 200.0},
        metric_name="3y_revenue_cagr",
    )
    assert res["status"] == "ok"
    assert res["value"] == 25.99
    assert res["formatted_value"] == "+25.99%"
    assert res["metric_name"] == "3y_revenue_cagr"


def test_custom_financial_metric_cagr_negative_base():
    from tools.finance_tools import compute_custom_financial_metric

    # Beginning value is negative
    res = compute_custom_financial_metric(
        expression="cagr(beginning_val, ending_val, 3)",
        context={"beginning_val": -50.0, "ending_val": 100.0},
        metric_name="net_income_cagr",
    )
    assert res["status"] == "ok"
    assert res["value"] is None
    assert res["formatted_value"] == "N/A (negative base period)"
    assert res["notes"] == "Base period value is non-positive"


def test_custom_financial_metric_zero_division():
    from tools.finance_tools import compute_custom_financial_metric

    res = compute_custom_financial_metric(
        expression="operating_cash_flow / market_cap",
        context={"operating_cash_flow": 500.0, "market_cap": 0.0},
        metric_name="fcf_yield",
    )
    assert res["status"] == "error"
    assert res["value"] is None
    assert res["formatted_value"] == "data unavailable"
    assert "ZeroDivisionError" in res["reason"]


def test_custom_financial_metric_missing_variable():
    from tools.finance_tools import compute_custom_financial_metric

    res = compute_custom_financial_metric(
        expression="operating_cash_flow - capex",
        context={"operating_cash_flow": 500.0},
        metric_name="fcf",
    )
    assert res["status"] == "error"
    assert res["value"] is None
    assert res["formatted_value"] == "data unavailable"
    assert "Missing required field" in res["reason"]
    assert "capex" in res["reason"]


def test_custom_financial_metric_ast_security_rejections():
    from tools.finance_tools import compute_custom_financial_metric

    # Reject __import__ or open
    res1 = compute_custom_financial_metric(
        expression="__import__('os').system('dir')",
        context={},
    )
    assert res1["status"] == "error"
    assert "security policy" in res1["reason"]

    # Reject attribute traversal like [].__class__
    res2 = compute_custom_financial_metric(
        expression="a.__class__",
        context={"a": 10},
    )
    assert res2["status"] == "error"
    assert "security policy" in res2["reason"]

    # Reject lambda
    res3 = compute_custom_financial_metric(
        expression="(lambda x: x + 1)(5)",
        context={},
    )
    assert res3["status"] == "error"
    assert "security policy" in res3["reason"]


def test_custom_financial_metric_fcf_yield_and_spread():
    from tools.finance_tools import compute_custom_financial_metric

    res = compute_custom_financial_metric(
        expression="fcf_yield(fcf, market_cap)",
        context={"fcf": 2500000000.0, "market_cap": 50000000000.0},
        metric_name="fcf_yield",
    )
    assert res["status"] == "ok"
    assert res["value"] == 5.0
    assert res["formatted_value"] == "5.00%"

    res_spread = compute_custom_financial_metric(
        expression="spread(return_on_capital, cost_of_capital)",
        context={"return_on_capital": 0.18, "cost_of_capital": 0.11},
        metric_name="economic_spread",
    )
    assert res_spread["status"] == "ok"
    assert res_spread["value"] == 7.0
    assert res_spread["formatted_value"] == "+7.00%"


def test_to_pct_normalizer():
    from tools.finance_tools import _to_pct

    # Strings with %
    assert _to_pct("0.39%") == 0.39
    assert _to_pct("75.83%") == 75.83
    assert _to_pct(" 100.0% ") == 100.0

    # Numeric fractions (< 1.0)
    assert _to_pct(0.0039) == 0.39
    assert _to_pct(0.7583) == 75.83

    # Numeric percentages (>= 1.0)
    assert _to_pct(75.83) == 75.83
    assert _to_pct(100.0) == 100.0

    # Zero and None
    assert _to_pct(0.0) == 0.0
    assert _to_pct(None) is None


def test_extract_holdings_jurisdiction_and_clamping():
    from unittest.mock import MagicMock
    from tools.finance_tools import _extract_holdings

    # Mock US equity (JPM)
    mock_jpm = MagicMock()
    mock_jpm.major_holders = pd.DataFrame(
        [
            ["0.39%", "% of Shares Held by All Insider"],
            ["75.83%", "% of Shares Held by Institutions"],
        ],
        columns=[0, 1],
    )
    mock_jpm.info = {"currency": "USD"}

    res_jpm = _extract_holdings(mock_jpm, ticker="JPM", currency="USD")
    assert res_jpm["is_us_equity"] is True
    assert res_jpm["insider_holding_label"] == "Insider Ownership (SEC Form 4/10-K)"
    assert res_jpm["promoter_holding_pct"] == 0.39
    assert res_jpm["fii_holding_pct"] == 75.83
    assert res_jpm["public_holding_pct"] == 23.78
    # Sum must mathematically equal 100.00%
    assert round(res_jpm["promoter_holding_pct"] + res_jpm["fii_holding_pct"] + res_jpm["public_holding_pct"], 2) == 100.00

    # Mock Indian equity (TCS)
    mock_tcs = MagicMock()
    mock_tcs.major_holders = pd.DataFrame(
        [
            ["71.79%", "% of Shares Held by All Insider"],
            ["17.46%", "% of Shares Held by Institutions"],
        ],
        columns=[0, 1],
    )
    mock_tcs.info = {"currency": "INR"}

    res_tcs = _extract_holdings(mock_tcs, ticker="TCS.NS", currency="INR")
    assert res_tcs["is_indian_equity"] is True
    assert res_tcs["insider_holding_label"] == "Promoter Holding"
    assert res_tcs["promoter_holding_pct"] == 71.79
    assert res_tcs["fii_holding_pct"] == 17.46
    assert res_tcs["public_holding_pct"] == 10.75
    assert round(res_tcs["promoter_holding_pct"] + res_tcs["fii_holding_pct"] + res_tcs["public_holding_pct"], 2) == 100.00


def test_banking_exclusions_and_debt_to_equity():
    from unittest.mock import MagicMock, patch
    from tools.finance_tools import get_valuation_multiples, get_fundamentals

    # Depository bank mock
    bank_info = {
        "sector": "Financial Services",
        "industry": "Banks - Diversified",
        "currency": "USD",
        "totalRevenue": 194910000000.0,
        "trailingPE": 12.5,
        "grossMargins": 0.0,
        "enterpriseToEbitda": None,
        "debtToEquity": None,
    }
    with patch("yfinance.Ticker") as mock_ticker_cls:
        mock_instance = MagicMock()
        mock_instance.info = bank_info
        mock_instance.recommendations = None
        mock_instance.recommendations_summary = None
        mock_instance.analyst_price_targets = None
        mock_ticker_cls.return_value = mock_instance

        val_res = get_valuation_multiples("JPM")
        assert val_res["is_bank_equity"] is True
        assert val_res["gross_margin"] is None
        assert "N/A (Depository Bank" in val_res["gross_margin_formatted"]
        assert "N/A (Depository Bank" in val_res["ev_ebitda_formatted"]

        fund_res = get_fundamentals("JPM")
        assert fund_res["is_bank_equity"] is True
        assert "Tier 1 Capital Governed" in fund_res["debt_to_equity_formatted"]

    # Corporate equity mock (TCS with raw debtToEquity = 10.21 -> 0.10x)
    corp_info = {
        "sector": "Technology",
        "industry": "Information Technology Services",
        "currency": "INR",
        "debtToEquity": 10.21,
    }
    with patch("yfinance.Ticker") as mock_ticker_cls:
        mock_instance = MagicMock()
        mock_instance.info = corp_info
        mock_instance.recommendations = None
        mock_instance.recommendations_summary = None
        mock_instance.analyst_price_targets = None
        mock_ticker_cls.return_value = mock_instance

        fund_res = get_fundamentals("TCS.NS")
        assert fund_res["debt_to_equity"] == 0.10
        assert fund_res["debt_to_equity_formatted"] == "0.10x (10.21%)"


def test_assemble_market_metrics_reconciliation():
    from tools.finance_tools import assemble_market_metrics
    from schemas import QuarterlyDataPoint
    from datetime import date

    # Trailing 4 quarters sum: 52.85 + 49.83 + 45.80 + 46.43 = 194.91B
    # Snapshot: 186.33B
    quarterly = [
        QuarterlyDataPoint(quarter="Q4 FY25", quarter_date=date(2025, 12, 31), revenue=52850000000.0, revenue_formatted="$52.85B"),
        QuarterlyDataPoint(quarter="Q3 FY25", quarter_date=date(2025, 9, 30), revenue=49830000000.0, revenue_formatted="$49.83B"),
        QuarterlyDataPoint(quarter="Q2 FY25", quarter_date=date(2025, 6, 30), revenue=45800000000.0, revenue_formatted="$45.80B"),
        QuarterlyDataPoint(quarter="Q1 FY25", quarter_date=date(2025, 3, 31), revenue=46430000000.0, revenue_formatted="$46.43B"),
    ]
    data = {
        "company_name": "JPMorgan Chase & Co.",
        "currency": "USD",
        "sector": "Financial Services",
        "industry": "Banks - Diversified",
        "current_price": 240.0,
        "revenue_ttm": 186330000000.0,
        "quarterly_financials": [q.model_dump() for q in quarterly],
    }
    metrics = assemble_market_metrics("JPM", data)
    assert metrics.is_bank_equity is True
    assert metrics.is_us_equity is True
    assert metrics.revenue_ttm == 194910000000.0
    assert metrics.ttm_reconciliation_note is not None
    assert "$194.91B" in metrics.ttm_reconciliation_note

