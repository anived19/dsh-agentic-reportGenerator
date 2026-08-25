"""
Unit and integration tests for the DSH Orchestrator (harness/dsh_orchestrator.py).

Tests cover:
  1. Orchestrator initialization and skill loading.
  2. Dynamic tool dispatching (finance, AML, search, calculation).
  3. Interactive multi-entity disambiguation (ask_user).
  4. Data validation and dynamic ReportSpec formatting.
  5. End-to-end report generation with trace dumping.
"""
from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock, patch
import pytest

from harness.dsh_orchestrator import DSHOrchestrator, run_dsh_orchestrator
from schemas import (
    AgentStatus,
    AMLSeverity,
    FinalReport,
    MarketMetrics,
    ReportType,
    SentimentFindings,
    SentimentLabel,
)


def test_dsh_orchestrator_initialization():
    """Verify state initialization and skill loading."""
    orchestrator = DSHOrchestrator(
        user_query="valuation of TCS",
        initial_company_ref="TCS",
        report_type=ReportType.VALUATION,
        run_aml=False,
        editorial_goal="Assess multi-year valuation multiples",
    )
    assert orchestrator.state.user_query == "valuation of TCS"
    assert orchestrator.state.company_reference == "TCS"
    assert orchestrator.state.report_type == ReportType.VALUATION
    assert orchestrator.state.run_aml is False
    assert orchestrator.state.editorial_goal == "Assess multi-year valuation multiples"
    assert len(orchestrator.skills) >= 10


def test_dsh_tool_dispatch_resolve_and_ask_user():
    """Test that resolving a conglomerate triggers interactive disambiguation."""
    user_choice_mock = MagicMock(return_value="Tata Consultancy Services (TCS.NS)")
    orchestrator = DSHOrchestrator(
        user_query="valuation of Tata",
        initial_company_ref="Tata",
        report_type=ReportType.VALUATION,
        interactive_fn=user_choice_mock,
    )

    # 1. Dispatch resolve_entity
    res, summary, ok, err = orchestrator._dispatch_tool("resolve_entity", {"query": "Tata"})
    assert ok is True
    assert len(orchestrator.state.candidate_entities) > 1

    # 2. Dispatch ask_user
    res_user, summary_user, ok_user, _ = orchestrator._dispatch_tool("ask_user", {"question": "Which company?", "options": []})
    assert ok_user is True
    assert user_choice_mock.called
    assert orchestrator.state.ticker == "TCS.NS"
    assert orchestrator.state.company_name == "Tata Consultancy Services"


def test_dsh_tool_dispatch_validation_and_planning():
    """Test validate_data, reflect_on_progress, and plan_report_format."""
    orchestrator = DSHOrchestrator(
        user_query="valuation of TCS",
        initial_company_ref="TCS",
        report_type=ReportType.VALUATION,
    )
    orchestrator.state.market_data = {
        "current_price": 4000.0,
        "pe_ratio": 28.5,
        "eps_ttm": 140.0,
    }

    # Validation
    val_res, _, ok, _ = orchestrator._dispatch_tool("validate_data", {})
    assert ok is True
    assert val_res["satisfied"] is True

    # Reflection
    ref_res, _, ok_ref, _ = orchestrator._dispatch_tool(
        "reflect_on_progress",
        {"gathered_summary": "Have all multiples", "still_needed": "", "next_action_rationale": "Ready to plan"},
    )
    assert ok_ref is True
    assert ref_res["checkpoint_recorded"] is True

    # Plan report format
    plan_res, _, ok_plan, _ = orchestrator._dispatch_tool(
        "plan_report_format",
        {
            "rationale": "Valuation focused",
            "sections": [
                {"name": "Executive Summary", "include": True, "order": 1},
                {"name": "Valuation Analysis", "include": True, "order": 2},
            ],
        },
    )
    assert ok_plan is True
    assert orchestrator.state.report_spec is not None
    assert len(orchestrator.state.report_spec.sections) == 2


def test_run_dsh_orchestrator_end_to_end(tmp_path):
    """Test full run_dsh_orchestrator pipeline execution with mocked LLM turn response."""
    with patch("harness.dsh_orchestrator.generate_with_retry") as mock_gemini, \
         patch("harness.dsh_orchestrator.run_chief_editor") as mock_editor, \
         patch("config.settings.output_dir", tmp_path):

        mock_editor.return_value = "# Executive Summary\nTCS is fundamentally strong."

        # Simulate Gemini issuing tool calls across turns
        call1 = MagicMock()
        call1.name = "resolve_entity"
        call1.args = {"query": "TCS"}

        call2 = MagicMock()
        call2.name = "get_price_snapshot"
        call2.args = {"ticker": "TCS.NS"}

        call3 = MagicMock()
        call3.name = "finalize_report"
        call3.args = {}

        resp1 = MagicMock()
        resp1.candidates = [MagicMock(content=MagicMock(parts=[]))]
        resp1.function_calls = [call1, call2]

        resp2 = MagicMock()
        resp2.candidates = [MagicMock(content=MagicMock(parts=[]))]
        resp2.function_calls = [call3]

        # For sentiment extraction call
        resp_sentiment = MagicMock()
        resp_sentiment.text = json.dumps({
            "overall_sentiment": "Bullish",
            "sentiment_summary": "Solid analyst ratings.",
            "key_catalysts": [{"claim": "AI growth", "source_url": "https://example.com"}],
            "key_risks": [{"claim": "IT spend slowdown", "source_url": "https://example.com"}],
        })

        mock_gemini.side_effect = [resp1, resp2, resp_sentiment]

        state, report = run_dsh_orchestrator(
            user_query="full equity report on TCS",
            initial_company_ref="TCS",
            report_type=ReportType.EQUITY,
            run_aml=False,
        )

        assert state.status == AgentStatus.DONE
        assert isinstance(report, FinalReport)
        assert report.ticker == "TCS.NS"
        assert report.company_name == "Tata Consultancy Services"
        assert "TCS is fundamentally strong" in report.markdown_body
        assert state.turn >= 2
        assert len(state.tool_log) >= 2

        # Verify trace file was written
        trace_files = list(tmp_path.glob("*_trace.json"))
        assert len(trace_files) >= 1
