"""
True DSH (DeepSeek Harness) Autonomous Agent Driver.

Orchestrates Stage 2 (Agentic Research) by spawning DeepSeek Harness with Cordis
composition and the stateful Finoscale MCP tool server over stdio.

DSH owns the entire multi-turn ReAct reasoning loop (Perceive -> Reason -> Act -> Observe).
Stage 3 (Synthesis) reads the empirical state gathered by DSH directly from the MCP session.
No data is ever fetched in parallel outside of DSH's MCP tool dispatches.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional

from config import settings
from harness.synthesis import render_aml_markdown, run_chief_editor
from schemas import (
    AgentState,
    AgentStatus,
    AMLFinding,
    AMLScreeningResult,
    AMLSeverity,
    CitedClaim,
    FinalReport,
    MarketMetrics,
    ReportSpec,
    ReportType,
    RunTelemetry,
    SectionSpec,
    SentimentFindings,
    SentimentLabel,
    ToolCallRecord,
    ValidationResult,
)
from tools.finance_tools import assemble_market_metrics

logger = logging.getLogger(__name__)


def ensure_dsh_environment() -> None:
    """Ensure DSH global configuration exists for Gemini provider routing."""
    dsh_dir = Path.home() / ".dsh"
    dsh_dir.mkdir(parents=True, exist_ok=True)
    settings_file = dsh_dir / "settings.yaml"

    settings_content = (
        "model: google:gemini-3.5-flash-lite\n"
        "llm-pi-ai:\n"
        "  providers:\n"
        "    google: {}\n"
    )
    settings_file.write_text(settings_content, encoding="utf-8")


def default_ask_user(question: str, options: list[str]) -> str:
    """Prompt the user on the real OS terminal for interactive disambiguation."""
    print(f"\n[INTERACTIVE DISAMBIGUATION] {question}")
    for idx, opt in enumerate(options, 1):
        print(f"  [{idx}] {opt}")
    while True:
        try:
            choice = input(f"\nSelect option number (1-{len(options)}) or type company name: ").strip()
            if not choice:
                continue
            if choice.isdigit():
                val = int(choice)
                if 1 <= val <= len(options):
                    selected = options[val - 1]
                    print(f"-> Selected: {selected}\n")
                    return selected
            for opt in options:
                if choice.lower() in opt.lower():
                    print(f"-> Selected: {opt}\n")
                    return opt
            print(f"Invalid choice '{choice}'. Please enter a number between 1 and {len(options)}.")
        except (EOFError, KeyboardInterrupt):
            print(f"\nDefaulting to first option: {options[0] if options else 'None'}")
            return options[0] if options else ""


def _find_npx_executable() -> str:
    """Locate the npx command on the system, resolving .cmd on Windows."""
    if sys.platform == "win32":
        cmd = shutil.which("npx.cmd") or shutil.which("npx")
        if cmd:
            return cmd
        # Common npm global path fallback
        appdata_npm = Path(os.environ.get("APPDATA", "")) / "npm" / "npx.cmd"
        if appdata_npm.exists():
            return str(appdata_npm)
        program_files_npm = Path(r"C:\Program Files\nodejs\npx.cmd")
        if program_files_npm.exists():
            return str(program_files_npm)
    else:
        cmd = shutil.which("npx")
        if cmd:
            return cmd
    return "npx"


def run_dsh_orchestrator(
    user_query: str,
    initial_company_ref: Optional[str] = None,
    report_type: ReportType = ReportType.GENERAL,
    run_aml: bool = False,
    editorial_goal: Optional[str] = None,
    interactive_fn: Optional[Callable[[str, list[str]], str]] = None,
) -> tuple[AgentState, FinalReport]:
    """
    Drive the report generation pipeline using DeepSeek Harness as the autonomous agent runtime.
    """
    start_time = time.time()
    ensure_dsh_environment()

    # 1. Initialize Session Directory & Metadata
    session_id = f"dsh_{int(time.time() * 1000)}"
    session_dir = settings.cache_dir / "sessions" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    # Set active session pointer
    active_session_file = settings.cache_dir / "sessions" / "active_session.json"
    active_session_file.write_text(json.dumps({"active_session_id": session_id}), encoding="utf-8")

    init_payload = {
        "session_id": session_id,
        "user_query": user_query,
        "company_reference": initial_company_ref,
        "report_type": report_type.value,
        "editorial_goal": editorial_goal or "Standard Comprehensive Financial Analysis",
        "run_aml": run_aml,
    }
    (session_dir / "session_init.json").write_text(json.dumps(init_payload, indent=2), encoding="utf-8")

    # 2. Build DSH Task Prompt
    task_prompt = (
        f"User Request: '{user_query}'\n"
        f"Detected Entity Prior: '{initial_company_ref or 'Unspecified'}'\n"
        f"Report Type: {report_type.value}\n"
        f"Editorial Goal: {editorial_goal or 'Standard Comprehensive Financial Analysis'}\n"
        f"AML Compliance Screening: {run_aml}\n\n"
        f"EXECUTION INSTRUCTIONS:\n"
        f"1. If company/ticker is not yet resolved, call mcp__finoscale__resolve_entity.\n"
        f"   If >1 candidate is returned, immediately call mcp__finoscale__ask_user.\n"
        f"2. Dynamically fetch required market data categories using MCP tools:\n"
        f"   - mcp__finoscale__get_price_snapshot\n"
        f"   - mcp__finoscale__get_valuation_multiples\n"
        f"   - mcp__finoscale__get_fundamentals\n"
        f"   - mcp__finoscale__get_quarterly_financials\n"
        f"   - mcp__finoscale__get_technicals\n"
        f"   - mcp__finoscale__get_ownership\n"
        f"   - mcp__finoscale__compute_custom_financial_metric (if ad-hoc formulas needed)\n"
        f"3. Call mcp__finoscale__search_web_news for live sentiment (max 3-5 searches).\n"
    )
    if run_aml:
        task_prompt += (
            f"4. Run compliance screening:\n"
            f"   - mcp__finoscale__run_structured_aml_sweep\n"
            f"   - mcp__finoscale__search_adverse_media\n"
        )
    task_prompt += (
        f"5. Call mcp__finoscale__reflect_on_progress to summarize gathered data.\n"
        f"6. Call mcp__finoscale__validate_data. Verify requirements are satisfied.\n"
        f"7. Call mcp__finoscale__plan_report_format with a tailored ReportSpec (max 5-7 sections).\n"
        f"8. Call mcp__finoscale__finalize_report to complete your execution."
    )

    # 3. Setup Process Environment
    env = os.environ.copy()
    env["FINOSCALE_SESSION_ID"] = session_id
    env["DSH_SESSION_ID"] = session_id
    env["GEMINI_API_KEY"] = settings.gemini_api_key
    env["GOOGLE_API_KEY"] = settings.gemini_api_key
    env["TAVILY_API_KEY"] = settings.tavily_api_key
    env["DSH_PERMISSION_MODE"] = "danger-full-access"

    npx_bin = _find_npx_executable()
    cordis_path = Path("cordis.yml").resolve()
    cmd = [npx_bin, "@deepseek-ai/dsh", "--profile", "headless", "--patch", str(cordis_path), task_prompt]

    print(f"\n[DSH Harness] Spawning DeepSeek Harness Agent Runtime (Session: {session_id})...")
    print(f"  -> Model Route: google:gemini-3.5-flash-lite (via @deepseek-ai/dsh-llm-pi-ai)")
    print(f"  -> Tools: Finoscale MCP stdio server (@deepseek-ai/dsh-mcp-client)")
    print(f"  -> ReAct Loop: Autonomous multi-turn reasoning owned by DSH\n")

    # 4. Execute DSH Subprocess with Real-Time Output & IPC ask_user Monitor
    ask_fn = interactive_fn or default_ask_user
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        env=env,
        shell=False,
    )

    # Background monitoring thread for ask_user IPC
    stop_monitor = threading.Event()

    def _monitor_ask_user():
        pending_file = session_dir / "ask_user_pending.json"
        response_file = session_dir / "ask_user_response.json"
        while not stop_monitor.is_set():
            if pending_file.exists():
                try:
                    p_data = json.loads(pending_file.read_text(encoding="utf-8"))
                    q_text = p_data.get("question", "Disambiguate entity:")
                    opts = p_data.get("options", [])
                    choice = ask_fn(q_text, opts)
                    response_file.write_text(json.dumps({"selected": choice}), encoding="utf-8")
                except Exception as exc:
                    logger.warning("Error handling ask_user IPC: %s", exc)
            time.sleep(0.1)

    monitor_thread = threading.Thread(target=_monitor_ask_user, daemon=True)
    monitor_thread.start()

    # Stream DSH output live to console
    dsh_stdout_lines: list[str] = []
    if proc.stdout:
        for line in iter(proc.stdout.readline, ""):
            line_str = line.rstrip()
            print(f"  [DSH] {line_str}", flush=True)
            dsh_stdout_lines.append(line_str)
        proc.stdout.close()

    proc.wait()
    stop_monitor.set()
    monitor_thread.join(timeout=1.0)

    # 5. Read Final Session State Output by MCP Server
    final_file = session_dir / "final_session.json"
    state_file = session_dir / "session_state.json"

    session_payload: Optional[dict[str, Any]] = None
    if final_file.exists():
        try:
            session_payload = json.loads(final_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to parse final_session.json: %s", exc)
    elif state_file.exists():
        try:
            session_payload = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to parse session_state.json: %s", exc)

    if not session_payload:
        raise RuntimeError(
            f"DSH execution failed: No session state was written to {session_dir}. "
            f"DSH exit code: {proc.returncode}"
        )

    # 6. Reconstruct AgentState from Empirical DSH Data
    state_status = AgentStatus(session_payload.get("status", "running"))
    if state_status != AgentStatus.DONE and proc.returncode != 0:
        raise RuntimeError(
            f"DSH session did not complete with finalize_report() (status: {state_status.value}, exit: {proc.returncode})"
        )

    market_data = session_payload.get("market_data", {})
    ticker = session_payload.get("ticker") or initial_company_ref or "TCS.NS"
    company_name = session_payload.get("company_name") or market_data.get("company_name") or ticker

    # Reconstruct ReportSpec
    report_spec = None
    if session_payload.get("report_spec"):
        try:
            report_spec = ReportSpec.model_validate(session_payload["report_spec"])
        except Exception:
            pass

    # Reconstruct SentimentFindings
    sentiment_findings = None
    if session_payload.get("sentiment_findings"):
        try:
            sentiment_findings = SentimentFindings.model_validate(session_payload["sentiment_findings"])
        except Exception:
            pass
    if not sentiment_findings:
        sentiment_findings = SentimentFindings(
            overall_sentiment=SentimentLabel.NEUTRAL,
            sentiment_summary=f"Market research analysis for {company_name}.",
            key_catalysts=[],
            key_risks=[],
        )

    # Reconstruct AMLScreeningResult
    aml_result = None
    if session_payload.get("aml_result"):
        try:
            aml_result = AMLScreeningResult.model_validate(session_payload["aml_result"])
        except Exception:
            pass

    # Reconstruct ToolLog & Telemetry
    tool_log = [ToolCallRecord.model_validate(t) for t in session_payload.get("tool_log", [])]
    telemetry = RunTelemetry.model_validate(session_payload.get("telemetry", {}))
    telemetry.wall_clock_seconds = round(time.time() - start_time, 2)

    state = AgentState(
        user_query=user_query,
        company_reference=initial_company_ref,
        ticker=ticker,
        company_name=company_name,
        report_type=report_type,
        editorial_goal=editorial_goal or session_payload.get("editorial_goal", "Standard Comprehensive Financial Analysis"),
        run_aml=run_aml,
        market_data=market_data,
        custom_metrics=session_payload.get("custom_metrics", {}),
        sentiment_findings=sentiment_findings,
        aml_result=aml_result,
        report_spec=report_spec,
        tool_log=tool_log,
        telemetry=telemetry,
        status=AgentStatus.DONE,
        turn=session_payload.get("turn", 1),
    )

    # 7. Assemble Grounded MarketMetrics from DSH Data (Zero Re-Fetching)
    market_metrics = assemble_market_metrics(ticker, market_data)

    # 8. Stage 3: Chief Editor Synthesis (Single-Shot Grounded Call)
    print("\n[Chief Editor] Synthesizing final research report from DSH empirical findings...")
    markdown_body = run_chief_editor(
        market_metrics=market_metrics,
        sentiment_findings=sentiment_findings,
        report_type=report_type,
        report_spec=report_spec,
        editorial_goal=state.editorial_goal,
        aml_result=aml_result if run_aml else None,
    )
    telemetry.gemini_calls += 1

    if run_aml and aml_result:
        aml_md = render_aml_markdown(aml_result)
        markdown_body = markdown_body + "\n\n" + aml_md

    # 9. KPI Cards Assembly
    kpi_cards: list[dict[str, str]] = []
    if market_metrics.current_price_formatted:
        kpi_cards.append({"label": "Current Price", "value": market_metrics.current_price_formatted, "note": "Market close"})
    if market_metrics.market_cap_formatted:
        kpi_cards.append({"label": "Market Cap", "value": market_metrics.market_cap_formatted, "note": "Scale"})
    if market_metrics.pe_ratio_formatted:
        kpi_cards.append({"label": "P/E Ratio", "value": market_metrics.pe_ratio_formatted, "note": "TTM multiple"})
    if market_metrics.roe_formatted:
        kpi_cards.append({"label": "Return on Equity", "value": market_metrics.roe_formatted, "note": "Profitability"})
    for cm_name, cm_val in state.custom_metrics.items():
        if isinstance(cm_val, dict) and cm_val.get("formatted_value") and cm_val.get("status") == "ok":
            kpi_cards.append({
                "label": cm_name.replace("_", " ").title(),
                "value": str(cm_val["formatted_value"]),
                "note": "Custom Metric",
            })

    final_report = FinalReport(
        ticker=ticker,
        company_name=company_name,
        report_type=report_type,
        editorial_goal=state.editorial_goal,
        markdown_body=markdown_body,
        market_metrics=market_metrics,
        sentiment_findings=sentiment_findings,
        aml_result=aml_result,
        report_spec=report_spec,
        telemetry=telemetry,
        kpi_cards=kpi_cards[:6],
    )

    # 10. Write Trace JSON File
    try:
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        ticker_slug = ticker.replace(".", "_").replace("/", "_")
        date_slug = date.today().isoformat()
        trace_path = settings.output_dir / f"{ticker_slug}_{date_slug}_trace.json"

        trace_data = {
            "session_id": session_id,
            "user_query": user_query,
            "ticker": ticker,
            "company_name": company_name,
            "report_type": report_type.value,
            "editorial_goal": state.editorial_goal,
            "run_aml": run_aml,
            "status": state.status.value,
            "turn": state.turn,
            "telemetry": telemetry.model_dump(),
            "report_spec": report_spec.model_dump() if report_spec else None,
            "custom_metrics": state.custom_metrics,
            "tool_log": [t.model_dump() for t in tool_log],
        }
        trace_path.write_text(json.dumps(trace_data, indent=2, default=str), encoding="utf-8")
        logger.info("Trace file written to %s", trace_path)
    except Exception as exc:
        logger.warning("Could not write trace file: %s", exc)

    return state, final_report
