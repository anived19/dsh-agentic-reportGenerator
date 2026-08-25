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
    """Verify async list_tools handler returns all 16 tools as Tool objects."""
    tools = asyncio.run(list_tools())
    assert len(tools) == 16
    names = [t.name for t in tools]
    assert "resolve_entity" in names
    assert "ask_user" in names
    assert "get_price_snapshot" in names
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
