"""
Unit tests for deterministic peer discovery and benchmarking tool:
- get_peer_tickers
"""
from unittest.mock import MagicMock, patch
import pytest

from tools.peer_resolver import get_peer_tickers


def test_get_peer_tickers_known_map():
    # Known banking map test
    res = get_peer_tickers("JPM", max_peers=3)
    assert res["target_ticker"] == "JPM"
    assert res["peers_count"] <= 3
    # Check that known peers like BAC, C, or WFC are present
    peer_symbols = [p["ticker"] for p in res["peers"]]
    assert any(s in peer_symbols for s in ["BAC", "C", "WFC", "GS", "MS"])
    assert "JPM" not in peer_symbols


def test_get_peer_tickers_indian_tech():
    # Known Indian tech map test
    res = get_peer_tickers("TCS.NS", max_peers=3)
    assert res["target_ticker"] == "TCS.NS"
    peer_symbols = [p["ticker"] for p in res["peers"]]
    assert any(s in peer_symbols for s in ["INFY.NS", "WIPRO.NS", "HCLTECH.NS", "LTIM.NS"])
    assert "TCS.NS" not in peer_symbols
