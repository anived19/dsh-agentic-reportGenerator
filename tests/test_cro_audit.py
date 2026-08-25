"""
Unit tests for Chief Risk Officer (CRO) deterministic audit verification:
- audit_draft_metrics
"""
import pytest
from tools.finance_tools import audit_draft_metrics


def test_cro_audit_passing():
    market_data = {
        "ticker": "JPM",
        "pe_ratio": 12.5,
        "pe_ratio_formatted": "12.50",
        "market_cap_formatted": "$650.00B",
        "promoter_holding_pct": 0.39,
        "fii_holding_pct": 75.83,
        "public_holding_pct": 23.78,
        "is_bank_equity": True,
        "gross_margin": None,
        "ev_ebitda": None,
    }
    res = audit_draft_metrics(market_data=market_data)
    assert res["audit_passed"] is True
    assert res["flags_count"] == 0
    assert "PASSED" in res["cro_verdict"]


def test_cro_audit_holdings_sum_failure():
    market_data = {
        "ticker": "BAD_CO",
        "promoter_holding_pct": 50.0,
        "fii_holding_pct": 40.0,
        "public_holding_pct": 20.0,  # 50 + 40 + 20 = 110 != 100
    }
    res = audit_draft_metrics(market_data=market_data)
    assert res["audit_passed"] is False
    assert res["flags_count"] > 0
    assert any("Holdings sum discrepancy" in d for d in res["discrepancies"])


def test_cro_audit_banking_exclusion_failure():
    market_data = {
        "ticker": "JPM",
        "is_bank_equity": True,
        "gross_margin": 0.45,  # Banks should not have Gross Margin
    }
    res = audit_draft_metrics(market_data=market_data)
    assert res["audit_passed"] is False
    assert any("Banking anomaly" in d for d in res["discrepancies"])
