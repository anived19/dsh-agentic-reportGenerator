"""
Comprehensive unit tests for the Stateful MCP Tool Server (harness/mcp_server.py).

Tests cover:
  1. Tool discovery and schema validation (all 16 tools defined).
  2. Direct dispatch to deterministic python functions and session state accumulation.
  3. validate_data completeness check against orchestrator_config profiles.
  4. reflect_on_progress and plan_report_format operations.
  5. finalize_report gating on validation satisfaction.
  6. Async list_tools and call_tool handlers via asyncio.run.
  7. Error handling on unknown or malformed tool requests.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from harness.mcp_server import (
    TOOL_DEFINITIONS,
    SessionStateManager,
    _dispatch_tool,
    call_tool,
    list_tools,
    session_mgr,
)
from schemas import AgentStatus, ReportType


def test_tool_definitions_registry():
    """Verify all 16 essential tools are defined in the MCP catalog."""
    tool_names = [t["name"] for t in TOOL_DEFINITIONS]
    expected_tools = [
        "resolve_entity",
        "ask_user",
        "get_price_snapshot",
        "get_valuation_multiples",
        "get_fundamentals",
        "get_quarterly_financials",
        "get_technicals",
        "get_ownership",
        "compute_custom_financial_metric",
        "search_web_news",
        "run_structured_aml_sweep",
        "search_adverse_media",
        "validate_data",
        "plan_report_format",
        "reflect_on_progress",
        "finalize_report",
    ]
    assert len(expected_tools) == 16
    for exp in expected_tools:
        assert exp in tool_names, f"Expected tool {exp} missing from MCP registry"
        tool_def = next(t for t in TOOL_DEFINITIONS if t["name"] == exp)
        assert "description" in tool_def
        assert "inputSchema" in tool_def
        assert tool_def["inputSchema"]["type"] == "object"


def test_dispatch_resolve_entity():
    """Test dispatch to resolve_entity and candidate storage."""
    result = _dispatch_tool("resolve_entity", {"query": "TCS"})
    assert isinstance(result, dict)
    assert result["candidate_count"] >= 1
    assert any(c["ticker"] == "TCS.NS" for c in result["candidates"])
    assert session_mgr.state.ticker == "TCS.NS"


def test_dispatch_data_accumulation():
    """Test that data tools accumulate findings into session_mgr.state.market_data."""
    session_mgr.state.market_data = {}
    
    res_price = _dispatch_tool("get_price_snapshot", {"ticker": "TCS.NS"})
    assert "current_price" in res_price
    assert "current_price" in session_mgr.state.market_data

    res_val = _dispatch_tool("get_valuation_multiples", {"ticker": "TCS.NS"})
    assert "pe_ratio" in res_val
    assert "pe_ratio" in session_mgr.state.market_data


def test_dispatch_validate_data_and_finalize_gating():
    """Test validation gating: finalize_report is refused when data requirements are missing."""
    session_mgr.state.report_type = ReportType.VALUATION
    session_mgr.state.market_data = {}  # Empty
    session_mgr.category_attempts = {k: 0 for k in session_mgr.category_attempts}

    # 1. Validation should fail (missing price_snapshot, valuation_multiples, fundamentals)
    val_res = _dispatch_tool("validate_data", {})
    assert val_res["satisfied"] is False
    assert "price_snapshot" in val_res["missing"]

    # 2. finalize_report should refuse
    fin_res = _dispatch_tool("finalize_report", {})
    assert "error" in fin_res
    assert "Cannot finalize report" in fin_res["error"]
    assert session_mgr.state.status != AgentStatus.DONE

    # 3. Populate required data & simulate news search
    session_mgr.state.market_data = {
        "current_price": 4000.0,
        "pe_ratio": 28.5,
        "eps_ttm": 140.0,
    }
    session_mgr.category_attempts["news_searches"] = 2

    val_res2 = _dispatch_tool("validate_data", {})
    assert val_res2["satisfied"] is True

    # 4. Now finalize_report should succeed
    fin_res2 = _dispatch_tool("finalize_report", {})
    assert fin_res2["status"] == "finalized"
    assert session_mgr.state.status == AgentStatus.DONE
    assert Path(fin_res2["final_payload_path"]).exists()


def test_dispatch_reflect_and_plan_report_format():
    """Test reflect_on_progress and plan_report_format."""
    ref_res = _dispatch_tool(
        "reflect_on_progress",
        {
            "gathered_summary": "All multiples collected",
            "still_needed": "Technicals",
            "next_action_rationale": "Fetch RSI-14",
        },
    )
    assert ref_res["status"] == "checkpoint_recorded"
    assert any(t.tool_name == "reflect_on_progress" for t in session_mgr.state.tool_log)

    plan_res = _dispatch_tool(
        "plan_report_format",
        {
            "rationale": "Focus on valuation & multiples",
            "sections": [
                {"name": "Executive Summary", "include": True, "order": 1},
                {"name": "Valuation Analysis", "include": True, "order": 2, "emphasis": "Lead with P/E and EV/EBITDA"},
            ],
        },
    )
    assert plan_res["status"] == "accepted"
    assert session_mgr.state.report_spec is not None
    assert len(session_mgr.state.report_spec.sections) == 2


def test_dispatch_unknown_tool():
    """Test dispatching an unknown tool raises ValueError."""
    with pytest.raises(ValueError, match="Unknown MCP tool: non_existent_tool"):
        _dispatch_tool("non_existent_tool", {})


def test_mcp_async_list_tools():
    """Verify async list_tools handler returns all 22 tools as Tool objects."""
    tools = asyncio.run(list_tools())
    assert len(tools) == 22
    names = [t.name for t in tools]
    assert "resolve_entity" in names
    assert "ask_user" in names
    assert "get_price_snapshot" in names
    assert "compute_banking_metrics" in names
    assert "compute_saas_metrics" in names
    assert "compute_retail_consumer_metrics" in names
    assert "get_peer_tickers" in names
    assert "investigate_financial_anomaly" in names
    assert "audit_draft" in names
    assert "validate_data" in names
    assert "plan_report_format" in names
    assert "finalize_report" in names


def test_mcp_async_call_tool_success():
    """Verify async call_tool successfully executes and serializes tool outputs."""
    contents = asyncio.run(call_tool("resolve_entity", {"query": "Infosys"}))
    assert len(contents) == 1
    assert contents[0].type == "text"
    data = json.loads(contents[0].text)
    assert isinstance(data, dict)
    assert data["candidate_count"] >= 1
    assert any(c.get("ticker") == "INFY.NS" for c in data["candidates"])


def test_mcp_async_call_tool_error_handling():
    """Verify call_tool catches exceptions and returns JSON error."""
    contents = asyncio.run(call_tool("non_existent_tool", {}))
    assert len(contents) == 1
    data = json.loads(contents[0].text)
    assert "error" in data


def test_aml_sweep_dispatch_produces_valid_state(monkeypatch):
    """Regression test: run_structured_aml_sweep's list[dict] return shape
    must round-trip through _dispatch_tool without crashing, and state.aml_result
    must be a valid AMLScreeningResult with entities_screened."""
    from unittest.mock import MagicMock
    from harness.mcp_server import _dispatch_tool, session_mgr
    from schemas import AMLFinding, AMLSeverity

    mock_findings = [
        AMLFinding(
            entity_screened="Test Corp",
            source_name="OFAC SDN",
            finding_summary="No confirmed match.",
            severity=AMLSeverity.NONE,
            source_url="https://sanctionssearch.ofac.treas.gov",
        ).model_dump()
    ]

    monkeypatch.setattr("tools.aml_tools.run_structured_aml_sweep", lambda **kw: mock_findings)

    result = _dispatch_tool("run_structured_aml_sweep", {"entity_name": "Test Corp", "ticker": "TEST.NS"})
    assert isinstance(result, list)
    assert session_mgr.state.aml_result is not None
    assert "Test Corp" in session_mgr.state.aml_result.entities_screened
    assert len(session_mgr.state.aml_result.findings) == 1


def test_session_state_hydration_on_restart(tmp_path, monkeypatch):
    """Regression test: SessionStateManager hydrates all previous state when session_state.json exists."""
    from harness.mcp_server import SessionStateManager
    from config import settings

    session_id = "test_hydrate_session_123"
    s_dir = tmp_path / "sessions" / session_id
    s_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "cache_dir", tmp_path)

    state_data = {
        "session_id": session_id,
        "ticker": "JPM",
        "company_name": "JPMorgan Chase & Co.",
        "user_query": "report on jpmorgan",
        "report_type": "general",
        "turn": 5,
        "market_data": {"current_price": 356.39, "pe_ratio": 15.06},
        "custom_metrics": {},
        "category_attempts": {"price_snapshot": 1, "news_searches": 2},
        "tool_log": [
            {"turn": 1, "tool_name": "get_price_snapshot", "arguments": {"ticker": "JPM"}, "result_summary": "ok", "ok": True}
        ],
    }
    (s_dir / "session_state.json").write_text(json.dumps(state_data), encoding="utf-8")

    mgr = SessionStateManager(session_id=session_id)
    assert mgr.state.ticker == "JPM"
    assert mgr.state.turn == 5
    assert mgr.state.market_data.get("pe_ratio") == 15.06
    assert mgr.category_attempts.get("news_searches") == 2
    assert len(mgr.state.tool_log) == 1


def test_validate_data_explicitly_reports_missing_news_searches():
    """Verify validate_data includes news_searches in missing list when min_news_searches is not met."""
    session_mgr.state.report_type = ReportType.GENERAL
    session_mgr.state.market_data = {
        "current_price": 356.39,  # required for general
    }
    session_mgr.category_attempts["price_snapshot"] = 1
    session_mgr.category_attempts["news_searches"] = 0

    val_res = _dispatch_tool("validate_data", {})
    assert val_res["satisfied"] is False
    assert any("news_searches" in item for item in val_res["missing"])
    assert "news_searches" in val_res["notes"]


def test_disambiguation_state_gate_and_lifecycle_reset(tmp_path, monkeypatch):
    """
    Verify that:
    1. resolve_entity with multiple candidates sets state.status = AWAITING_USER and clears ticker.
    2. Data fetching tools are blocked while AWAITING_USER.
    3. ask_user resolves the entity, resets candidate_entities to [], and transitions status back to RUNNING.
    4. Subsequent data fetching proceeds without errors.
    """
    from harness.mcp_server import _dispatch_tool, session_mgr
    from schemas import AgentStatus

    # 1. Resolve entity with multiple candidates
    res = _dispatch_tool("resolve_entity", {"query": "tata"})
    assert res["candidate_count"] > 1
    assert session_mgr.state.status == AgentStatus.AWAITING_USER
    assert session_mgr.state.ticker is None
    assert len(session_mgr.state.candidate_entities) > 1

    # 2. Block data fetching tools
    blocked_res = _dispatch_tool("get_price_snapshot", {})
    assert "error" in blocked_res
    assert "Entity disambiguation required" in blocked_res["error"]

    # 3. Simulate ask_user selection with instantaneous IPC response
    def fake_sleep(dur):
        resp_file = session_mgr.session_dir / "ask_user_response.json"
        resp_file.write_text(json.dumps({"selected": "Tata Motors Limited (TATAMOTORS.NS)"}), encoding="utf-8")

    monkeypatch.setattr("time.sleep", fake_sleep)
    ask_res = _dispatch_tool("ask_user", {"question": "Which entity?", "options": ["Tata Motors Limited (TATAMOTORS.NS)"]})
    assert session_mgr.state.ticker == "TATAMOTORS.NS"
    assert session_mgr.state.status == AgentStatus.RUNNING
    assert session_mgr.state.candidate_entities == []

    # 4. Data fetching is now unlocked
    with patch("tools.finance_tools.get_price_snapshot") as mock_price:
        mock_price.return_value = {"current_price": 750.0, "currency": "INR"}
        price_res = _dispatch_tool("get_price_snapshot", {})
        assert price_res.get("current_price") == 750.0


def test_new_sector_peer_cro_tools_dispatch():
    """Verify dispatching and state updates for sector, peer, anomaly, and audit tools."""
    from harness.mcp_server import _dispatch_tool, session_mgr

    session_mgr.state.ticker = "JPM"
    session_mgr.state.company_name = "JPMorgan Chase & Co."

    # 1. Sector tool dispatch
    with patch("tools.finance_tools.compute_banking_metrics") as mock_bank:
        mock_bank.return_value = {"ticker": "JPM", "nim_pct": 2.5, "efficiency_ratio_pct": 55.0}
        res = _dispatch_tool("compute_banking_metrics", {"ticker": "JPM"})
        assert res["nim_pct"] == 2.5
        assert session_mgr.state.sector_metrics is not None
        assert session_mgr.state.sector_metrics.banking.nim_pct == 2.5

    # 2. Peer benchmarking dispatch
    with patch("tools.peer_resolver.get_peer_tickers") as mock_peers:
        mock_peers.return_value = {
            "target_ticker": "JPM",
            "target_name": "JPMorgan Chase & Co.",
            "industry": "Banks - Diversified",
            "peers_count": 2,
            "peers": [
                {"ticker": "BAC", "name": "Bank of America Corp", "pe_ratio": 11.2},
                {"ticker": "WFC", "name": "Wells Fargo & Co", "pe_ratio": 10.5},
            ],
            "industry_summary": "2 peers",
        }
        res = _dispatch_tool("get_peer_tickers", {"ticker": "JPM", "max_peers": 2})
        assert res["peers_count"] == 2
        assert session_mgr.state.peer_benchmarks is not None
        assert len(session_mgr.state.peer_benchmarks.peers) == 2

    # 3. Anomaly hunting dispatch
    with patch("tools.search_tools.investigate_financial_anomaly") as mock_anom:
        mock_anom.return_value = {
            "ticker": "JPM",
            "anomaly_type": "QoQ Profit Drop",
            "findings_count": 1,
            "findings": [
                {
                    "anomaly_type": "QoQ Profit Drop",
                    "metric_impacted": "Net Income",
                    "observed_value": "$10B",
                    "prior_or_expected_value": "$14B",
                    "driver_explanation": "One-off FDIC special assessment fee of $2.9B.",
                    "source_url": "https://sec.gov/jpm-10k",
                    "severity": "high",
                }
            ],
            "summary": "1 finding",
        }
        res = _dispatch_tool("investigate_financial_anomaly", {"ticker": "JPM", "anomaly_type": "QoQ Profit Drop"})
        assert res["findings_count"] == 1
        assert len(session_mgr.state.anomaly_findings) >= 1
        assert session_mgr.state.anomaly_findings[-1].metric_impacted == "Net Income"

    # 4. CRO Audit dispatch
    with patch("tools.finance_tools.audit_draft_metrics") as mock_audit:
        mock_audit.return_value = {
            "audit_passed": True,
            "flags_count": 0,
            "verified_metrics_count": 5,
            "discrepancies": [],
            "cro_verdict": "PASSED",
        }
        res = _dispatch_tool("audit_draft", {"draft_summary": "Test draft summary"})
        assert res["audit_passed"] is True
        assert session_mgr.state.cro_audit_report is not None
        assert session_mgr.state.cro_audit_report.audit_passed is True


