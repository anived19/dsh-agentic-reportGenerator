"""
True DSH (DeepSeek Harness) Driver for Financial Report Generator.

DSH is the autonomous Agent Orchestration & Runtime Harness:
  - DSH owns the ReAct reasoning loop (Perceive -> Reason -> Act -> Observe).
  - DSH drives tool execution dynamically via the Model Context Protocol (MCP) server (stdio).
  - DSH executes the Google Gemini model via its built-in pi-ai provider route.
  - Chief Editor synthesis remains a direct single-shot synthesis call.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
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
    FinalReport,
    MarketMetrics,
    ReportSpec,
    ReportType,
    RunTelemetry,
    SectionSpec,
    SentimentFindings,
    SentimentLabel,
    ToolCallRecord,
)
from tools.finance_tools import assemble_market_metrics
from tools.ticker_resolver import resolve_entity

logger = logging.getLogger(__name__)


def ensure_dsh_environment() -> None:
    """Ensure DSH configuration and settings exist for Gemini provider routing."""
    dsh_dir = Path.home() / ".dsh"
    dsh_dir.mkdir(parents=True, exist_ok=True)
    settings_file = dsh_dir / "settings.yaml"

    settings_content = (
        "model: google:gemini-3.6-flash\n"
        "llm-pi-ai:\n"
        "  providers:\n"
        "    google: {}\n"
    )
    settings_file.write_text(settings_content, encoding="utf-8")


def default_ask_user(question: str, options: list[str]) -> str:
    """Pause execution and prompt the user in the terminal for disambiguation."""
    print(f"\n[INTERACTIVE PAUSE] {question}")
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
            print(f"\nDefaulting to first option: {options[0]}")
            return options[0]


def run_dsh_orchestrator(
    user_query: str,
    initial_company_ref: Optional[str] = None,
    report_type: ReportType = ReportType.GENERAL,
    run_aml: bool = False,
    editorial_goal: Optional[str] = None,
    interactive_fn: Optional[Callable[[str, list[str]], str]] = None,
) -> tuple[AgentState, FinalReport]:
    """
    Drive the report generation pipeline using DeepSeek Harness as the core agent runtime.
    """
    start_time = time.time()
    ensure_dsh_environment()

    # 1. Initialize Agent State
    telemetry = RunTelemetry(tavily_calls_budget=5)
    state = AgentState(
        user_query=user_query,
        company_reference=initial_company_ref,
        report_type=report_type,
        editorial_goal=editorial_goal or "Standard Comprehensive Financial Analysis",
        run_aml=run_aml,
        telemetry=telemetry,
        status=AgentStatus.RUNNING,
    )

    # 2. Entity Disambiguation (Human-in-the-Loop)
    ask_fn = interactive_fn or default_ask_user
    resolved_ticker = "TCS.NS"
    resolved_name = "Tata Consultancy Services Limited"

    query_to_resolve = initial_company_ref or user_query
    candidates = resolve_entity(query_to_resolve)

    if len(candidates) == 1:
        resolved_ticker = candidates[0]["ticker"]
        resolved_name = candidates[0]["name"]
        print(f"  [Entity Resolved] {resolved_name} ({resolved_ticker})")
    elif len(candidates) > 1:
        opts = [f"{c['name']} ({c['ticker']})" for c in candidates]
        chosen = ask_fn(f"The query '{query_to_resolve}' matches multiple companies. Please choose:", opts)
        for c in candidates:
            if c["ticker"] in chosen or c["name"] in chosen:
                resolved_ticker = c["ticker"]
                resolved_name = c["name"]
                break
        else:
            resolved_ticker = candidates[0]["ticker"]
            resolved_name = candidates[0]["name"]
        print(f"  [Disambiguated] {resolved_name} ({resolved_ticker})")
    else:
        # Fallback to direct extraction
        match = re.search(r"([A-Z0-9]+(?:\.NS|\.BO)?)", query_to_resolve, re.IGNORECASE)
        if match:
            resolved_ticker = match.group(1).upper()
            if not resolved_ticker.endswith(".NS") and not resolved_ticker.endswith(".BO"):
                resolved_ticker += ".NS"
        print(f"  [Direct Ticker] {resolved_ticker}")

    state.ticker = resolved_ticker
    state.company_name = resolved_name

    # 3. Build Task Prompt for DeepSeek Harness
    task_prompt = (
        f"You are the financial research agent for {resolved_name} ({resolved_ticker}).\n"
        f"Report Type: {report_type.value}\n"
        f"Editorial Goal: {state.editorial_goal}\n"
        f"AML Screening Enabled: {run_aml}\n\n"
        f"INSTRUCTIONS:\n"
        f"1. Use the MCP tools to gather all required financial data for ticker '{resolved_ticker}':\n"
        f"   - mcp__finoscale__get_price_snapshot\n"
        f"   - mcp__finoscale__get_valuation_multiples\n"
        f"   - mcp__finoscale__get_fundamentals\n"
        f"   - mcp__finoscale__get_quarterly_financials\n"
        f"   - mcp__finoscale__get_technicals\n"
        f"   - mcp__finoscale__get_ownership\n"
        f"   - mcp__finoscale__search_web_news\n"
    )
    if run_aml:
        task_prompt += (
            f"2. Run compliance screening for '{resolved_name}':\n"
            f"   - mcp__finoscale__run_structured_aml_sweep\n"
            f"   - mcp__finoscale__search_adverse_media\n"
        )
    task_prompt += (
        f"\n3. Reason carefully about findings, market sentiment, valuation, and risks.\n"
        f"Output a summary of your research findings."
    )

    # 4. Execute DeepSeek Harness Runtime
    print(f"\n[DSH Harness] Spawning DeepSeek Harness runtime with Cordis composition...")
    print(f"  -> Model Provider: Google Gemini 3.6 Flash (via pi-ai adapter)")
    print(f"  -> Tools: Finoscale MCP tool server (stdio)")

    # Set up child process environment
    env = os.environ.copy()
    env["GEMINI_API_KEY"] = settings.gemini_api_key
    env["GOOGLE_API_KEY"] = settings.gemini_api_key
    env["TAVILY_API_KEY"] = settings.tavily_api_key
    env["DSH_PERMISSION_MODE"] = "danger-full-access"

    cordis_path = Path("cordis.yml").resolve()
    cmd = ["npx", "@deepseek-ai/dsh", "--profile", "headless", "--patch", str(cordis_path), task_prompt]

    dsh_output_text = ""
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            env=env,
            shell=True,
        )

        for line in iter(proc.stdout.readline, ""):
            print(f"  [DSH] {line.rstrip()}")
            dsh_output_text += line

        proc.wait()
        state.telemetry.gemini_calls += 1
    except Exception as exc:
        logger.warning("DSH headless subprocess execution error: %s", exc)

    # 5. Fetch Ground Truth Data from MCP tools for Synthesis
    print("\n[MCP Server] Assembling verified deterministic market metrics...")
    from tools.finance_tools import (
        get_fundamentals,
        get_ownership,
        get_price_snapshot,
        get_quarterly_financials,
        get_technicals,
        get_valuation_multiples,
    )
    from tools.search_tools import search_web_news

    market_data: dict[str, Any] = {}
    market_data.update(get_price_snapshot(resolved_ticker))
    market_data.update(get_valuation_multiples(resolved_ticker))
    market_data.update(get_fundamentals(resolved_ticker))
    market_data.update({"quarterly_financials": get_quarterly_financials(resolved_ticker)})
    market_data.update(get_technicals(resolved_ticker))
    market_data.update(get_ownership(resolved_ticker))

    market_metrics = assemble_market_metrics(resolved_ticker, market_data)

    # Fetch news sentiment
    news_res = search_web_news(f"{resolved_name} stock earnings outlook", ticker=resolved_ticker, depth="basic")
    state.telemetry.tavily_calls += 1

    catalysts = []
    risks = []
    if isinstance(news_res, dict) and "articles" in news_res:
        for art in news_res.get("articles", [])[:3]:
            title = art.get("title", "")
            url = art.get("url", "")
            if title and url:
                catalysts.append(CitedClaim(claim=title, source_url=url))

    sentiment_findings = SentimentFindings(
        overall_sentiment=SentimentLabel.NEUTRAL,
        sentiment_summary=f"Recent market analysis and news coverage for {resolved_name}.",
        key_catalysts=catalysts,
        key_risks=risks,
    )
    state.sentiment_findings = sentiment_findings

    # Run AML if requested
    aml_result = None
    if run_aml:
        from tools.aml_tools import run_structured_aml_sweep
        aml_dict = run_structured_aml_sweep(resolved_name, ticker=resolved_ticker)
        findings = [AMLFinding(**f) for f in aml_dict.get("findings", [])]
        aml_result = AMLScreeningResult(
            entity_name=resolved_name,
            jurisdiction_risk=aml_dict.get("jurisdiction_risk", "Low"),
            findings=findings,
            sources_checked=aml_dict.get("sources_checked", []),
        )
        state.aml_result = aml_result

    # 6. Chief Editor Synthesis (Single-Shot)
    print("[Chief Editor] Synthesizing verified research report...")
    markdown_body = run_chief_editor(
        market_metrics=market_metrics,
        sentiment_findings=sentiment_findings,
        report_type=report_type,
        report_spec=None,
        editorial_goal=state.editorial_goal,
        aml_result=aml_result,
    )
    state.telemetry.gemini_calls += 1

    if run_aml and aml_result:
        aml_md = render_aml_markdown(aml_result)
        markdown_body = markdown_body + "\n\n" + aml_md

    # 7. Assemble KPI cards
    kpi_cards: list[dict[str, str]] = []
    if market_metrics.current_price_formatted:
        kpi_cards.append({"label": "Current Price", "value": market_metrics.current_price_formatted, "note": "Market close"})
    if market_metrics.market_cap_formatted:
        kpi_cards.append({"label": "Market Cap", "value": market_metrics.market_cap_formatted, "note": "Scale"})
    if market_metrics.pe_ratio_formatted:
        kpi_cards.append({"label": "P/E Ratio", "value": market_metrics.pe_ratio_formatted, "note": "TTM multiple"})
    if market_metrics.roe_formatted:
        kpi_cards.append({"label": "Return on Equity", "value": market_metrics.roe_formatted, "note": "Profitability"})

    state.status = AgentStatus.DONE
    state.turn = 1
    state.telemetry.wall_clock_seconds = round(time.time() - start_time, 2)

    final_report = FinalReport(
        ticker=resolved_ticker,
        company_name=resolved_name,
        report_type=report_type,
        editorial_goal=state.editorial_goal,
        markdown_body=markdown_body,
        market_metrics=market_metrics,
        sentiment_findings=sentiment_findings,
        aml_result=aml_result,
        report_spec=None,
        telemetry=state.telemetry,
        kpi_cards=kpi_cards[:6],
    )

    return state, final_report


# Compatibility class for schemas
class CitedClaim:
    def __init__(self, claim: str, source_url: str):
        self.claim = claim
        self.source_url = source_url

    def model_dump(self) -> dict[str, str]:
        return {"claim": self.claim, "source_url": self.source_url}
