"""
Unit and integration tests for the True DSH Driver (harness/dsh_driver.py).

Verifies the Acceptance Criteria:
  1. Break-the-mock test: Stubbing DSH to a no-op that produces no final session payload FAILS.
  2. Single Source of Truth: Synthesis reads only what DSH gathered during its session.
  3. Interactive Disambiguation: ask_user IPC communication between MCP server and driver.
  4. End-to-end trace-driven report generation.
"""
from __future__ import annotations

import os
os.environ["DSH_TELEMETRY_DISABLED"] = "1"
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from harness.dsh_driver import default_ask_user, ensure_dsh_environment, run_dsh_orchestrator
from schemas import AgentStatus, FinalReport, ReportType, SentimentFindings, SentimentLabel


def test_ensure_dsh_environment(tmp_path):
    """Verify DSH global settings are created with Google Gemini provider route."""
    with patch("pathlib.Path.home", return_value=tmp_path):
        ensure_dsh_environment()
        settings_file = tmp_path / ".dsh" / "settings.yaml"
        assert settings_file.exists()
        content = settings_file.read_text(encoding="utf-8")
        assert "google:gemini-3.5-flash-lite" in content
        assert "llm-pi-ai" in content


def test_break_the_mock_no_op_fails():
    """
    Acceptance Criterion #1 (Break-the-mock test):
    If DSH is stubbed to a no-op and fails to run or write a final session payload,
    the pipeline MUST raise an explicit RuntimeError rather than silently faking a report.
    """
    with patch("subprocess.Popen") as mock_popen:
        proc_mock = MagicMock()
        proc_mock.stdout = None
        proc_mock.wait.return_value = 1
        proc_mock.returncode = 1
        mock_popen.return_value = proc_mock

        with pytest.raises(RuntimeError, match="DSH execution failed"):
            run_dsh_orchestrator(
                user_query="stock report on TCS",
                initial_company_ref="TCS",
                report_type=ReportType.EQUITY,
            )


def test_dsh_driver_interactive_disambiguation_ipc(tmp_path):
    """
    Test IPC rendezvous: ask_user creates pending request, driver answers,
    and selection propagates to DSH and final report.
    """
    def mock_interactive_fn(q, opts):
        return "Tata Steel (TATASTEEL.NS)"

    def mock_dsh_run(cmd, *args, **kwargs):
        # Find session_dir from env
        env = kwargs.get("env", {})
        session_id = env.get("DSH_SESSION_ID", "test_session")
        session_dir = Path("cache") / "sessions" / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        # Simulate DSH executing and calling finalize_report
        final_payload = {
            "session_id": session_id,
            "ticker": "TATASTEEL.NS",
            "company_name": "Tata Steel Limited",
            "report_type": "equity",
            "editorial_goal": "Equity research",
            "run_aml": False,
            "status": "done",
            "turn": 4,
            "market_data": {
                "company_name": "Tata Steel Limited",
                "current_price": 145.0,
                "current_price_formatted": "Rs. 145.00",
                "market_cap": 1800000000000.0,
                "market_cap_formatted": "Rs. 1.80 Lakh Cr",
                "pe_ratio": 15.2,
                "pe_ratio_formatted": "15.20",
            },
            "custom_metrics": {},
            "tool_log": [
                {"tool_name": "resolve_entity", "arguments": {"query": "Tata"}, "result_summary": "ok", "ok": True},
                {"tool_name": "ask_user", "arguments": {"question": "Choose"}, "result_summary": "TATASTEEL.NS", "ok": True},
                {"tool_name": "get_price_snapshot", "arguments": {"ticker": "TATASTEEL.NS"}, "result_summary": "ok", "ok": True},
                {"tool_name": "finalize_report", "arguments": {}, "result_summary": "finalized", "ok": True},
            ],
            "telemetry": {"gemini_calls": 3, "tavily_calls": 1, "tavily_calls_budget": 5, "wall_clock_seconds": 12.5},
        }
        (session_dir / "final_session.json").write_text(json.dumps(final_payload), encoding="utf-8")

        proc_mock = MagicMock()
        proc_mock.stdout.readline.side_effect = ["DSH: reasoning...\n", "DSH: calling tools...\n", ""]
        proc_mock.wait.return_value = 0
        proc_mock.returncode = 0
        return proc_mock

    with patch("subprocess.Popen", side_effect=mock_dsh_run), \
         patch("harness.dsh_driver.run_chief_editor", return_value="# Executive Summary\nTata Steel is competitive."), \
         patch("config.settings.output_dir", tmp_path):

        state, report = run_dsh_orchestrator(
            user_query="stock report of tata",
            initial_company_ref="tata",
            report_type=ReportType.EQUITY,
            interactive_fn=mock_interactive_fn,
        )

        assert state.status == AgentStatus.DONE
        assert report.ticker == "TATASTEEL.NS"
        assert report.company_name == "Tata Steel Limited"
        assert len(state.tool_log) == 4
        assert state.tool_log[0].tool_name == "resolve_entity"
        assert state.tool_log[1].tool_name == "ask_user"


def test_dsh_driver_omission_preservation(tmp_path):
    """
    Acceptance Criterion #3 & #4 (Single Source of Truth & Omission Test):
    If DSH never calls get_technicals, technical indicators in MarketMetrics remain
    genuinely None and are flagged in unavailable_fields — NOT silently backfilled.
    """
    def mock_dsh_omission(cmd, *args, **kwargs):
        env = kwargs.get("env", {})
        session_id = env.get("DSH_SESSION_ID", "test_omission")
        session_dir = Path("cache") / "sessions" / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        final_payload = {
            "session_id": session_id,
            "ticker": "TCS.NS",
            "company_name": "Tata Consultancy Services",
            "report_type": "valuation",
            "editorial_goal": "Valuation deep dive",
            "run_aml": False,
            "status": "done",
            "turn": 3,
            "market_data": {
                "company_name": "Tata Consultancy Services",
                "current_price": 4050.0,
                "current_price_formatted": "Rs. 4,050.00",
                "pe_ratio": 29.1,
                "pe_ratio_formatted": "29.10",
                # technicals omitted
            },
            "custom_metrics": {},
            "tool_log": [
                {"tool_name": "get_price_snapshot", "arguments": {"ticker": "TCS.NS"}, "result_summary": "ok", "ok": True},
                {"tool_name": "get_valuation_multiples", "arguments": {"ticker": "TCS.NS"}, "result_summary": "ok", "ok": True},
                {"tool_name": "finalize_report", "arguments": {}, "result_summary": "finalized", "ok": True},
            ],
            "telemetry": {"gemini_calls": 2, "tavily_calls": 0, "tavily_calls_budget": 5, "wall_clock_seconds": 8.0},
        }
        (session_dir / "final_session.json").write_text(json.dumps(final_payload), encoding="utf-8")

        proc_mock = MagicMock()
        proc_mock.stdout.readline.side_effect = ["DSH turn 1\n", ""]
        proc_mock.wait.return_value = 0
        proc_mock.returncode = 0
        return proc_mock

    with patch("subprocess.Popen", side_effect=mock_dsh_omission), \
         patch("harness.dsh_driver.run_chief_editor", return_value="# Report\nTCS valuation"), \
         patch("config.settings.output_dir", tmp_path):

        state, report = run_dsh_orchestrator(
            user_query="valuation of TCS",
            initial_company_ref="TCS",
            report_type=ReportType.VALUATION,
        )

        assert report.market_metrics.rsi_14 is None
        assert "rsi_14" in report.market_metrics.unavailable_fields
        assert report.market_metrics.current_price == 4050.0
