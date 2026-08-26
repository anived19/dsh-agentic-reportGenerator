import pytest
from tools.finance_tools import cross_check_source_agreement

def test_cross_check_source_agreement_match():
    yfinance_data = {
        "market_cap": 15000000000000, # 15 lakh crores (absolute)
        "promoter_holding_pct": 0.723 # 72.3%
    }
    moneycontrol_data = {
        "market_cap_cr": 1500000,
        "promoter_holding_pct": "72.30%"
    }
    
    res = cross_check_source_agreement(yfinance_data, moneycontrol_data)
    assert res["mismatches_found"] is False
    assert len(res["mismatches"]) == 0

def test_cross_check_source_agreement_mismatch():
    yfinance_data = {
        "market_cap": 15000000000000, # 15 lakh crores
        "promoter_holding_pct": 0.60 # 60%
    }
    moneycontrol_data = {
        "market_cap_cr": 1300000, # 13 lakh crores (diff > 5%)
        "promoter_holding_pct": "72.30%" # Diff > 5%
    }
    
    res = cross_check_source_agreement(yfinance_data, moneycontrol_data)
    assert res["mismatches_found"] is True
    assert len(res["mismatches"]) == 2
    
    mc_mismatch = next(m for m in res["mismatches"] if m["field"] == "market_cap")
    assert mc_mismatch["status"] == "MISMATCH"
    assert mc_mismatch["diff_pct"] > 5.0

def test_cross_check_source_agreement_missing():
    yfinance_data = {
        "market_cap": None,
        "promoter_holding_pct": 0.723
    }
    moneycontrol_data = {
        "market_cap_cr": 1500000,
        "promoter_holding_pct": None
    }
    
    res = cross_check_source_agreement(yfinance_data, moneycontrol_data)
    assert res["mismatches_found"] is True
    assert len(res["mismatches"]) == 2
    
    mc_mismatch = next(m for m in res["mismatches"] if m["field"] == "market_cap")
    assert mc_mismatch["status"] == "DATA_MISSING_IN_ONE_SOURCE"

def test_cross_check_source_agreement_zero_division():
    yfinance_data = {
        "market_cap": 15000000000000,
        "promoter_holding_pct": 0.0 # 0% ITC Ltd
    }
    moneycontrol_data = {
        "market_cap_cr": 1500000,
        "promoter_holding_pct": "0.00%"
    }
    
    res = cross_check_source_agreement(yfinance_data, moneycontrol_data)
    assert res["mismatches_found"] is False
