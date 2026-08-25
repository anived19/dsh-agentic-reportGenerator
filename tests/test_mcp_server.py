"""
Unit tests for the MCP Tool Server (harness/mcp_server.py).

Tests cover:
  1. Tool discovery and schema validation (all 10 tools defined).
  2. Direct dispatch to deterministic python functions.
  3. Async list_tools and call_tool handlers via asyncio.run.
  4. Error handling on unknown or malformed tool requests.
"""
from __future__ import annotations

import asyncio
import json
import pytest
from unittest.mock import MagicMock, patch

from harness.mcp_server import TOOL_DEFINITIONS, _dispatch_tool, call_tool, list_tools


def test_tool_definitions_registry():
    """Verify all 10 essential financial, AML, and search tools are defined."""
    tool_names = [t["name"] for t in TOOL_DEFINITIONS]
    expected_tools = [
        "resolve_entity",
        "get_price_snapshot",
        "get_valuation_multiples",
        "get_fundamentals",
        "get_quarterly_financials",
        "get_technicals",
        "get_ownership",
        "search_web_news",
        "run_structured_aml_sweep",
        "search_adverse_media",
    ]
    for exp in expected_tools:
        assert exp in tool_names, f"Expected tool {exp} missing from MCP registry"
        tool_def = next(t for t in TOOL_DEFINITIONS if t["name"] == exp)
        assert "description" in tool_def
        assert "inputSchema" in tool_def
        assert tool_def["inputSchema"]["type"] == "object"


def test_dispatch_resolve_entity():
    """Test dispatch to resolve_entity."""
    result = _dispatch_tool("resolve_entity", {"query": "TCS"})
    assert isinstance(result, list)
    assert len(result) >= 1
    assert result[0]["ticker"] == "TCS.NS"


def test_dispatch_unknown_tool():
    """Test dispatching an unknown tool raises ValueError."""
    with pytest.raises(ValueError, match="Unknown tool: non_existent_tool"):
        _dispatch_tool("non_existent_tool", {})


def test_mcp_async_list_tools():
    """Verify async list_tools handler returns all tools as Tool objects."""
    tools = asyncio.run(list_tools())
    assert len(tools) == len(TOOL_DEFINITIONS)
    names = [t.name for t in tools]
    assert "resolve_entity" in names
    assert "get_price_snapshot" in names
    assert "run_structured_aml_sweep" in names


def test_mcp_async_call_tool_success():
    """Verify async call_tool successfully executes and serializes tool outputs."""
    contents = asyncio.run(call_tool("resolve_entity", {"query": "Infosys"}))
    assert len(contents) == 1
    assert contents[0].type == "text"
    data = json.loads(contents[0].text)
    assert isinstance(data, list)
    assert any(c.get("ticker") == "INFY.NS" for c in data)


def test_mcp_async_call_tool_error_handling():
    """Verify call_tool catches exceptions and returns JSON error."""
    contents = asyncio.run(call_tool("non_existent_tool", {}))
    assert len(contents) == 1
    data = json.loads(contents[0].text)
    assert "error" in data
