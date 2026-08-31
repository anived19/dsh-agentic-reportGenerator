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
from dotenv import load_dotenv, find_dotenv

# Force load .env from project root
load_dotenv(find_dotenv(), override=True)

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
    session_id: Optional[str] = None,
) -> tuple[AgentState, FinalReport]:
    """
    Drive the report generation pipeline using DeepSeek Harness as the autonomous agent runtime.
    """
    start_time = time.time()
    ensure_dsh_environment()

    # 1. Initialize Session Directory & Metadata
    session_id = session_id or f"dsh_{int(time.time() * 1000)}"
    session_dir = settings.cache_dir / "sessions" / session_id
    
    is_resume = session_dir.exists() and (session_dir / "session_state.json").exists()
    
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
    resume_block = ""
    if is_resume:
        resume_block = (
            f"[SYSTEM: RESUME FROM CRASH/TIMEOUT]\n"
            f"You are resuming session {session_id} which was interrupted.\n"
            f"DO NOT start from scratch. First, call mcp__finoscale__reflect_on_progress \n"
            f"to see what has already been completed in this session's state, then pick up exactly \n"
            f"where you left off.\n\n"
        )

    task_prompt = (
        f"/agent-teams Produce a financial research report for {initial_company_ref or user_query}.\n\n"
        f"Create a team. Add two members: 'market-data' (role: financial data analyst) and "
        f"'aml-media' (role: compliance and sentiment analyst). Create two tasks with no "
        f"dependencies between them — one for market-data covering price snapshot, valuation, "
        f"fundamentals, technicals, ownership, sector metrics, and peer benchmarking; one for "
        f"aml-media covering the structured AML sweep and adverse-media/news search — so they "
        f"can run concurrently. Then add a 'synthesis' member and create a task for it that "
        f"depends on both prior tasks: build the section index, then spawn four scorer members "
        f"('finance-scorer', 'banking-scorer', 'business-scorer', 'hygiene-scorer') each with "
        f"one task depending on the section-index task, using the credit-report-format skill to "
        f"assemble the final 13-section report. Wait for every task to complete, then present the "
        f"consolidated markdown and call finalize_report.\n"
    )

    # 3. Setup Process Environment
    child_env = os.environ.copy()
    if not child_env.get("DSH_TELEMETRY_DISABLED"):
        raise RuntimeError("Strict telemetry lockdown failed: DSH_TELEMETRY_DISABLED must be set in the environment before spawning DSH.")
    child_env["FINOSCALE_SESSION_ID"] = session_id
    child_env["DSH_SESSION_ID"] = session_id
    child_env["GEMINI_API_KEY"] = settings.gemini_api_key
    child_env["GOOGLE_API_KEY"] = settings.gemini_api_key
    child_env["TAVILY_API_KEY"] = settings.tavily_api_key
    child_env["DSH_PERMISSION_MODE"] = "danger-full-access"

    npx_bin = _find_npx_executable()
    cordis_path = Path("cordis.yml").resolve()
    cmd = [npx_bin, "@deepseek-ai/dsh@0.1.2-alpha.2", "--profile", "headless", "--patch", str(cordis_path), task_prompt]

    print(f"\n[DSH Harness] Spawning DeepSeek Harness Agent Runtime (Session: {session_id})...")
    print(f"  -> Model Route: google:gemini-3.5-flash-lite (via @deepseek-ai/dsh-llm-pi-ai)")
    print(f"  -> Tools: Finoscale MCP stdio server (@deepseek-ai/dsh-mcp-client)")
    print(f"  -> ReAct Loop: Autonomous multi-turn reasoning owned by DSH\n")

    # 4. Execute DSH Subprocess with Real-Time Output & IPC ask_user Monitor
    ask_fn = interactive_fn or default_ask_user
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stdin=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        env=child_env,
        shell=False,
    )

    # Background monitoring thread for ask_user IPC
    stop_monitor = threading.Event()

    def _monitor_ask_user():
        pending_file = session_dir / "ask_user_pending.json"
        response_file = session_dir / "ask_user_response.json"
        analyst_pending_file = session_dir / "analyst_review_pending.json"
        analyst_response_file = session_dir / "analyst_review_response.json"
        
        while not stop_monitor.is_set():
            if pending_file.exists():
                try:
                    p_data = json.loads(pending_file.read_text(encoding="utf-8"))
                    q_text = p_data.get("question", "Disambiguate entity:")
                    opts = p_data.get("options", [])
                    choice = ask_fn(q_text, opts)
                    response_file.write_text(json.dumps({"selected": choice}), encoding="utf-8")
                    pending_file.rename(session_dir / "ask_user_pending.json.processed")
                except Exception as exc:
                    logger.warning("Error handling ask_user IPC: %s", exc)
                    
            if analyst_pending_file.exists() and proc.stdin:
                try:
                    data = json.loads(analyst_pending_file.read_text(encoding="utf-8"))
                    summary = data.get("draft_summary", "")
                    print(f"\n\n[ANALYST REVIEW REQUIRED]")
                    print(f"Draft Summary:\n{summary}\n")
                    approval = input("Approve this draft? (yes/no): ").strip().lower()
                    status = "approved" if approval in ("y", "yes") else "rejected"
                    
                    # Inject the response back into DSH via stdin (which becomes a user message in DSH)
                    msg = f"SYSTEM INJECTION: Analyst review {status}. Update your status and proceed accordingly."
                    proc.stdin.write(msg + "\n")
                    proc.stdin.flush()
                    
                    analyst_response_file.write_text(json.dumps({"status": status}), encoding="utf-8")
                    analyst_pending_file.rename(session_dir / "analyst_review_pending.json.processed")
                except Exception as exc:
                    logger.warning("Error handling analyst review IPC: %s", exc)
                    
            time.sleep(0.1)

    monitor_thread = threading.Thread(target=_monitor_ask_user, daemon=True)
    monitor_thread.start()

    # Stream DSH output live to console
    dsh_stdout_lines: list[str] = []
    if proc.stdout:
        for line in iter(proc.stdout.readline, ""):
            line_str = line.rstrip()
            try:
                print(f"  [DSH] {line_str}", flush=True)
            except UnicodeEncodeError:
                print(f"  [DSH] {line_str.encode('ascii', 'replace').decode('ascii')}", flush=True)
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
    
    # Verify that the session actually finalized properly
    finalized = any(t.get("tool_name") == "finalize_report" and t.get("ok") for t in session_payload.get("tool_log", []))
    
    if not finalized or session_payload.get("status") != "done":
        raise RuntimeError(
            f"DSH session {session_id} exited prematurely at turn {session_payload.get('turn')} "
            f"without calling finalize_report. Halting synthesis to prevent empty PDF generation."
        )

    market_data = session_payload.get("market_data", {})
    ticker = session_payload.get("ticker") or initial_company_ref
    if not ticker:
        raise RuntimeError("DSH session completed without resolving a valid ticker symbol.")
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

    # Reconstruct ScoreCategoryResults
    from schemas import ScoreCategoryResult
    score_results = []
    if session_payload.get("score_results"):
        try:
            score_results = [
                ScoreCategoryResult.model_validate(r)
                for r in session_payload.get("score_results", [])
            ]
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

    # Forward session state containers to market_data
    if "peer_benchmarks" not in market_data and session_payload.get("peer_benchmarks"):
        market_data["peer_benchmarks"] = session_payload["peer_benchmarks"]
    if "sector_metrics" not in market_data and session_payload.get("sector_metrics"):
        market_data["sector_metrics"] = session_payload["sector_metrics"]
    if "anomaly_findings" not in market_data and session_payload.get("anomaly_findings"):
        market_data["anomaly_findings"] = session_payload["anomaly_findings"]
    if "cro_audit_report" not in market_data and session_payload.get("cro_audit_report"):
        market_data["cro_audit_report"] = session_payload["cro_audit_report"]

    # Guarantee peer benchmarks are populated if not already present
    if not market_data.get("peer_benchmarks"):
        try:
            from tools.peer_resolver import get_peer_tickers
            market_data["peer_benchmarks"] = get_peer_tickers(ticker)
        except Exception as peer_exc:
            logger.debug("Automatic peer resolution fallback failed for %s: %s", ticker, peer_exc)

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
        score_results=score_results,
    )
    telemetry.gemini_calls += 1

    if run_aml and aml_result:
        aml_md = render_aml_markdown(aml_result)
        markdown_body = markdown_body + "\n\n" + aml_md
        
    if score_results:
        from harness.synthesis import render_credit_scoring_markdown
        score_md = render_credit_scoring_markdown(score_results)
        if score_md:
            markdown_body = markdown_body + "\n\n" + score_md

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
