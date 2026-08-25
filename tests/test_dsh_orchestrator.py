"""
Unit and integration tests for the True DSH Driver (harness/dsh_driver.py).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from harness.dsh_driver import default_ask_user, ensure_dsh_environment, run_dsh_orchestrator
from schemas import AgentStatus, FinalReport, ReportType


def test_ensure_dsh_environment(tmp_path):
    """Test that DSH environment settings are written."""
    with patch("pathlib.Path.home", return_value=tmp_path):
        ensure_dsh_environment()
        settings_file = tmp_path / ".dsh" / "settings.yaml"
        assert settings_file.exists()
        content = settings_file.read_text(encoding="utf-8")
        assert "google:gemini-3.6-flash" in content
        assert "llm-pi-ai" in content


def test_dsh_driver_interactive_disambiguation():
    """Test disambiguation prompt when resolving multiple candidate entities."""
    interactive_mock = MagicMock(return_value="Tata Steel (TATASTEEL.NS)")
    
    with patch("subprocess.Popen") as mock_popen, \
         patch("harness.dsh_driver.run_chief_editor", return_value="# Report\nTata Steel content"):
        
        proc_mock = MagicMock()
        proc_mock.stdout.readline.side_effect = ["DSH turn 1\n", ""]
        proc_mock.wait.return_value = 0
        mock_popen.return_value = proc_mock

        state, report = run_dsh_orchestrator(
            user_query="stock report of tata",
            initial_company_ref="tata",
            report_type=ReportType.EQUITY,
            interactive_fn=interactive_mock,
        )

        assert interactive_mock.called
        assert report.ticker == "TATASTEEL.NS"
        assert state.status == AgentStatus.DONE


def test_run_dsh_orchestrator_end_to_end(tmp_path):
    """Test full DSH driver pipeline execution."""
    with patch("subprocess.Popen") as mock_popen, \
         patch("harness.dsh_driver.run_chief_editor", return_value="# Executive Summary\nTCS is strong."), \
         patch("config.settings.output_dir", tmp_path):

        proc_mock = MagicMock()
        proc_mock.stdout.readline.side_effect = ["DSH step 1: calling tools...\n", ""]
        proc_mock.wait.return_value = 0
        mock_popen.return_value = proc_mock

        state, report = run_dsh_orchestrator(
            user_query="full equity report on TCS",
            initial_company_ref="TCS",
            report_type=ReportType.EQUITY,
            run_aml=False,
        )

        assert state.status == AgentStatus.DONE
        assert isinstance(report, FinalReport)
        assert report.ticker == "TCS.NS"
        assert "TCS is strong" in report.markdown_body
