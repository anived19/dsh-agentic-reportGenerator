"""
DSH (DeepSeek Harness) Agentic Orchestrator for the Financial Report Generator.

Implements the complete, genuine multi-turn ReAct reasoning loop:
  Perceive -> Reason (with Chain-of-Thought) -> Act (MCP Tools) -> Observe -> Reflect -> Plan -> Finalize

The LLM is in 100% control of all planning and tool dispatch decisions.
Tools provide deterministic ground-truth observations (zero hallucinated figures).
Chief Editor synthesis remains a direct single-shot google-genai call.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional

import yaml
from google import genai
from google.genai import types
from pydantic import ValidationError

from config import settings
from harness.gemini_retry import generate_with_retry
from harness.md_loader import SkillBundle, load_agent_prompt, load_skill
from harness.synthesis import render_aml_markdown, run_chief_editor
from schemas import (
    AgentState,
    AgentStatus,
    AMLFinding,
    AMLScreeningResult,
    AMLSeverity,
    ClarificationRequest,
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
from tools.finance_tools import assemble_market_metrics, compute_custom_financial_metric
from tools.ticker_resolver import resolve_entity

logger = logging.getLogger(__name__)

# 12 RPM target -> 60.0 / 12.0 = 5.0 seconds pacing interval
_GEMINI_PACING_INTERVAL = 3.0
_last_gemini_call_timestamp = 0.0


def _pace_gemini_call() -> None:
    """Enforce pacing headroom before issuing Gemini calls."""
    global _last_gemini_call_timestamp
    elapsed = time.time() - _last_gemini_call_timestamp
    if elapsed < _GEMINI_PACING_INTERVAL:
        sleep_dur = _GEMINI_PACING_INTERVAL - elapsed
        time.sleep(sleep_dur)
    _last_gemini_call_timestamp = time.time()


def _sentiment_response_json_schema() -> dict:
    """Schema for extracting structured SentimentFindings from accumulated news."""
    cited_claim = {
        "type": "object",
        "properties": {
            "claim": {"type": "string"},
            "source_url": {"type": "string"},
        },
        "required": ["claim", "source_url"],
    }
    return {
        "type": "object",
        "properties": {
            "overall_sentiment": {"type": "string", "enum": ["Bullish", "Bearish", "Neutral"]},
            "sentiment_summary": {"type": "string"},
            "key_catalysts": {"type": "array", "items": cited_claim},
            "key_risks": {"type": "array", "items": cited_claim},
        },
        "required": ["overall_sentiment", "sentiment_summary", "key_catalysts", "key_risks"],
    }


def _extract_sentiment_from_search(
    client: genai.Client,
    search_records: list[dict[str, Any]],
    queries_used: list[str],
) -> SentimentFindings:
    """Extract structured sentiment findings from accumulated search data."""
    if not search_records:
        return SentimentFindings(
            overall_sentiment=SentimentLabel.NEUTRAL,
            sentiment_summary="No news searches were conducted during this run.",
            queries_used=[],
            extraction_failed=False,
        )

    context_lines = []
    for item in search_records:
        title = item.get("title", "")
        url = item.get("url", "")
        content = item.get("content", "")
        context_lines.append(f"Title: {title}\nURL: {url}\nSnippet: {content}\n---")

    context_text = "\n".join(context_lines)
    prompt = (
        "Based on the following retrieved news items, perform a financial sentiment analysis "
        "and output JSON conforming to the schema.\n\n"
        f"{context_text}\n\n"
        "CRITICAL CURRENCY INSTRUCTION: Use the target company's native reporting currency. "
        "For Indian companies (e.g. .NS, .BO, Tata, TCS, Infosys, Reliance), use Rs. / INR / Cr / Lakhs — "
        "NEVER substitute USD '$' or '$ billion' for Indian rupee values unless the source explicitly discusses USD amounts."
    )

    schema = _sentiment_response_json_schema()
    try:
        _pace_gemini_call()
        response = generate_with_retry(
            client,
            model=settings.gemini_model,
            contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_json_schema=schema,
            ),
        )
        findings = SentimentFindings.model_validate_json(response.text)
        findings.queries_used = queries_used
        return findings
    except Exception as exc:
        logger.warning("Structured sentiment extraction error: %s", exc)
        return SentimentFindings(
            overall_sentiment=SentimentLabel.NEUTRAL,
            sentiment_summary="Automated sentiment extraction encountered an error parsing search results.",
            queries_used=queries_used,
            extraction_failed=True,
        )


def default_ask_user(question: str, options: list[str]) -> str:
    """Prompt user in terminal to interactively select an entity."""
    print(f"\n[INTERACTIVE PAUSE] {question}")
    for idx, opt in enumerate(options, 1):
        print(f"  [{idx}] {opt}")
    
    while True:
        try:
            choice = input(f"\nSelect option number (1-{len(options)}) or type company name: ").strip()
            if not choice:
                continue
            if choice.isdigit():
                num = int(choice)
                if 1 <= num <= len(options):
                    selected = options[num - 1]
                    print(f"  -> Selected: {selected}\n")
                    return selected
            # Substring match
            for opt in options:
                if choice.lower() in opt.lower():
                    print(f"  -> Selected: {opt}\n")
                    return opt
            print(f"Invalid input '{choice}'. Please enter a number between 1 and {len(options)}.")
        except (EOFError, KeyboardInterrupt):
            print("\nSelection cancelled. Defaulting to first option.")
            return options[0] if options else ""


class DSHOrchestrator:
    """
    Genuine Agentic ReAct Orchestrator.
    Drives a multi-turn Perceive -> Reason -> Act -> Observe loop using Gemini
    with Thinking/CoT enabled and all tools exposed via standard schemas.
    """

    def __init__(
        self,
        user_query: str,
        initial_company_ref: Optional[str] = None,
        report_type: ReportType = ReportType.GENERAL,
        run_aml: bool = False,
        editorial_goal: Optional[str] = None,
        interactive_fn: Optional[Callable[[str, list[str]], str]] = None,
    ):
        self.state = AgentState(
            user_query=user_query,
            company_reference=initial_company_ref,
            report_type=report_type,
            editorial_goal=editorial_goal,
            run_aml=run_aml,
            telemetry=RunTelemetry(),
        )
        self.interactive_fn = interactive_fn or default_ask_user
        self.skills: dict[str, SkillBundle] = {}
        self._load_all_skills()

        self.search_records: list[dict[str, Any]] = []
        self.search_queries_used: list[str] = []
        self.cached_tool_results: dict[str, Any] = {}

    def _load_all_skills(self) -> None:
        """Load all skill declarations and callables from skills/*.md."""
        skill_names = [
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
            "reflect_on_progress",
            "validate_data",
            "plan_report_format",
            "finalize_report",
        ]
        for name in skill_names:
            try:
                bundle = load_skill(name)
                self.skills[bundle.name] = bundle
            except Exception as exc:
                logger.warning("Could not load skill %s: %s", name, exc)

    def _dispatch_tool(self, name: str, args: dict[str, Any]) -> tuple[Any, str, bool, Optional[str]]:
        """Dispatch tool execution and update state."""
        from harness.mcp_server import _dispatch_tool as mcp_dispatch

        try:
            if name == "ask_user":
                question = args.get("question", "Please clarify:")
                options = args.get("options", [])
                if not options and self.state.candidate_entities:
                    options = [f"{c['name']} ({c['ticker']})" for c in self.state.candidate_entities]

                print(f"\n[INTERACTIVE PAUSE] {question}")
                for idx, opt in enumerate(options, 1):
                    print(f"  [{idx}] {opt}")

                selection = self.interactive_fn(question, options)
                for c in self.state.candidate_entities:
                    if c.get("ticker", "") in selection or c.get("name", "") in selection:
                        self.state.ticker = c["ticker"]
                        self.state.company_name = c["name"]
                        break
                if not self.state.ticker and self.state.candidate_entities:
                    self.state.ticker = self.state.candidate_entities[0]["ticker"]
                    self.state.company_name = self.state.candidate_entities[0]["name"]

                return {"selected": selection, "ticker": self.state.ticker}, f"User selected: {selection}", True, None

            elif name == "validate_data":
                missing = []
                m = self.state.market_data
                if not m.get("current_price"):
                    missing.append("price_snapshot")
                if not m.get("pe_ratio") and not m.get("revenue_ttm"):
                    missing.append("valuation_multiples")
                if not m.get("eps_ttm") and not m.get("roe"):
                    missing.append("fundamentals")

                satisfied = len(missing) == 0
                val_res = {
                    "satisfied": satisfied,
                    "missing": missing,
                    "ticker": self.state.ticker,
                    "message": "All essential data categories present." if satisfied else f"Missing categories: {missing}",
                }
                return val_res, f"Validation satisfied={satisfied}", True, None

            elif name == "plan_report_format":
                rationale = args.get("rationale", "Standard framing")
                raw_sections = args.get("sections", [])
                sec_specs = []
                for s in raw_sections:
                    if isinstance(s, dict):
                        raw_name = s.get("name") or s.get("title") or s.get("key") or "section"
                        sec_key = s.get("key") or str(raw_name).lower().replace(" ", "_").replace("-", "_")
                        sec_specs.append(
                            SectionSpec(
                                key=sec_key,
                                title=s.get("title") or s.get("name") or sec_key.replace("_", " ").title(),
                                include=s.get("include", True),
                                order=s.get("order", 1),
                                emphasis=s.get("emphasis", "Standard analysis"),
                            )
                        )
                self.state.report_spec = ReportSpec(rationale=rationale, sections=sec_specs)
                return {"status": "ok", "sections_count": len(sec_specs)}, f"Planned {len(sec_specs)} sections", True, None

            elif name == "reflect_on_progress":
                summary = args.get("gathered_summary", "")
                still_needed = args.get("still_needed", "")
                rationale = args.get("next_action_rationale", "")
                return {"status": "ok", "checkpoint_recorded": True}, f"Reflected: {still_needed or 'Ready'}", True, None

            elif name == "finalize_report":
                self.state.status = AgentStatus.DONE
                return {"status": "ok", "ready_for_synthesis": True}, "Finalize triggered. Execution complete.", True, None

            elif name == "compute_custom_financial_metric":
                expr = args.get("expression", "")
                t_sym = args.get("ticker", self.state.ticker or "TCS.NS")
                m_name = args.get("metric_name", "custom_metric")
                ctx = args.get("context", {})
                res = compute_custom_financial_metric(expr, t_sym, m_name, ctx)
                self.state.custom_metrics[m_name] = res
                return res, f"Computed {m_name}: {res.get('formatted_value')}", True, None

            else:
                # Dispatch through standard MCP tool handler
                raw_result = mcp_dispatch(name, args)

                if name == "resolve_entity":
                    if isinstance(raw_result, list):
                        self.state.candidate_entities = raw_result
                        if len(raw_result) == 1:
                            self.state.ticker = raw_result[0].get("ticker")
                            self.state.company_name = raw_result[0].get("name")
                    return raw_result, f"Found {len(raw_result) if isinstance(raw_result, list) else 1} candidate(s)", True, None

                elif name in (
                    "get_price_snapshot",
                    "get_valuation_multiples",
                    "get_fundamentals",
                    "get_technicals",
                    "get_ownership",
                ):
                    if isinstance(raw_result, dict):
                        self.state.market_data.update(raw_result)
                        if raw_result.get("company_name") and not self.state.company_name:
                            self.state.company_name = raw_result["company_name"]
                    return raw_result, f"Fetched {name} successfully", True, None

                elif name == "get_quarterly_financials":
                    if isinstance(raw_result, list):
                        self.state.market_data["quarterly_financials"] = raw_result
                    return raw_result, f"Fetched {len(raw_result)} quarterly points", True, None

                elif name == "search_web_news":
                    self.state.telemetry.tavily_calls += 1
                    if isinstance(raw_result, list):
                        self.search_records.extend(raw_result)
                        self.search_queries_used.append(args.get("query", ""))
                    return raw_result, f"Retrieved {len(raw_result)} news results", True, None

                elif name in ("run_structured_aml_sweep", "search_adverse_media"):
                    self.state.telemetry.tavily_calls += 1
                    if isinstance(raw_result, list):
                        findings = [AMLFinding.model_validate(f) for f in raw_result]
                        highest_sev = AMLSeverity.NONE
                        for f in findings:
                            if f.severity == AMLSeverity.HIGH:
                                highest_sev = AMLSeverity.HIGH
                                break
                            elif f.severity == AMLSeverity.ELEVATED and highest_sev != AMLSeverity.HIGH:
                                highest_sev = AMLSeverity.ELEVATED
                            elif f.severity == AMLSeverity.WATCH and highest_sev not in (AMLSeverity.HIGH, AMLSeverity.ELEVATED):
                                highest_sev = AMLSeverity.WATCH

                        if not self.state.aml_result:
                            self.state.aml_result = AMLScreeningResult(
                                entity_screened=self.state.company_name or self.state.ticker or "Target Entity",
                                overall_severity=highest_sev,
                                findings=findings,
                                screening_date=date.today(),
                            )
                        else:
                            self.state.aml_result.findings.extend(findings)
                    return raw_result, f"AML {name} completed", True, None

                else:
                    return raw_result, f"Executed {name}", True, None

        except Exception as exc:
            logger.warning("Tool %s execution failed: %s", name, exc)
            return None, f"Tool error: {exc}", False, str(exc)

    def run(self) -> tuple[AgentState, FinalReport]:
        """Execute the genuine multi-turn ReAct reasoning loop."""
        start_time = time.time()
        client = genai.Client(api_key=settings.gemini_api_key)
        system_prompt = load_agent_prompt("orchestrator")

        # Expose all 16 skills as callable tools for Gemini
        declarations = [s.declaration for s in self.skills.values()]
        tools = [types.Tool(function_declarations=declarations)]

        thinking_config = None
        try:
            thinking_config = types.ThinkingConfig(
                include_thoughts=True,
                thinking_budget=1024,
            )
        except Exception:
            pass

        loop_config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=tools,
            thinking_config=thinking_config,
            max_output_tokens=4096,
        )

        user_initial_text = (
            f"User request: {self.state.user_query}\n"
            f"Detected Prior Company Reference: {self.state.company_reference or 'Unspecified'}\n"
            f"Detected Prior Report Type: {self.state.report_type.value}\n"
            f"Editorial Goal / Framing: {self.state.editorial_goal or 'Standard Comprehensive Financial Analysis'}\n"
            f"AML Compliance Screening Enabled: {self.state.run_aml}\n\n"
            f"Instructions:\n"
            f"1. Begin by calling resolve_entity with the company reference.\n"
            f"2. If resolve_entity returns >1 candidate, immediately call ask_user.\n"
            f"3. Dynamically fetch required market data categories.\n"
            f"4. Search news/adverse media within the Tavily budget.\n"
            f"5. Call reflect_on_progress(), validate_data(), plan_report_format(), and finalize_report()."
        )

        contents: list[types.Content] = [
            types.Content(role="user", parts=[types.Part(text=user_initial_text)])
        ]

        logger.info("Starting Master Agentic ReAct Loop (Perceive -> Reason -> Act -> Observe)...")

        while self.state.turn < self.state.max_turns and self.state.status == AgentStatus.RUNNING:
            self.state.turn += 1
            print(f"\n[Turn {self.state.turn}/{self.state.max_turns}] Reasoning & Perceiving...")

            _pace_gemini_call()
            self.state.telemetry.gemini_calls += 1

            try:
                response = generate_with_retry(
                    client,
                    model=settings.gemini_model,
                    contents=contents,
                    config=loop_config,
                )
            except Exception as exc:
                if loop_config.thinking_config is not None:
                    logger.warning("Gemini call failed with ThinkingConfig; retrying without thinking_config...")
                    loop_config.thinking_config = None
                    response = generate_with_retry(
                        client,
                        model=settings.gemini_model,
                        contents=contents,
                        config=loop_config,
                    )
                else:
                    logger.error("Gemini turn %d failed: %s", self.state.turn, exc)
                    break

            if response.candidates and response.candidates[0].content:
                contents.append(response.candidates[0].content)

            # Extract reasoning / thoughts
            thought_parts: list[str] = []
            text_parts: list[str] = []

            if response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
                for p in response.candidates[0].content.parts:
                    p_thought = getattr(p, "thought", None)
                    p_text = getattr(p, "text", None)
                    if p_thought is True and p_text:
                        thought_parts.append(p_text.strip())
                    elif isinstance(p_thought, str) and p_thought.strip():
                        thought_parts.append(p_thought.strip())
                    elif p_text and p_text.strip():
                        text_parts.append(p_text.strip())

            thought_text = " ".join(thought_parts).strip()
            rationale_text = " ".join(text_parts).strip()

            if thought_text:
                print(f"\n  [Thought / Agent Chain of Thought]\n    {thought_text}\n")
            if rationale_text:
                print(f"  [Reasoning & Planning]\n    {rationale_text}\n")

            calls = response.function_calls or []
            if not calls:
                # Bounce back to agent if it didn't issue tool calls
                if self.state.status == AgentStatus.DONE:
                    break
                contents.append(types.Content(
                    role="user",
                    parts=[types.Part(text="Continue your execution flow. Explicitly call reflect_on_progress(), validate_data(), plan_report_format(), and finalize_report().")]
                ))
                continue

            function_response_parts = []
            for call in calls:
                args = call.args or {}
                print(f"  [Act: Tool Call] {call.name}({args})")

                result_payload, summary, ok, error = self._dispatch_tool(call.name, args)
                print(f"  [Observe] {summary}")

                record = ToolCallRecord(
                    turn=self.state.turn,
                    tool_name=call.name,
                    arguments=args,
                    result_summary=summary,
                    ok=ok,
                    error=error,
                    reasoning_text=rationale_text or thought_text or None,
                )
                self.state.tool_log.append(record)

                if self.state.status == AgentStatus.DONE:
                    break

                function_response_parts.append(
                    types.Part.from_function_response(
                        name=call.name,
                        response={"result": result_payload} if ok else {"error": error}
                    )
                )

            if self.state.status == AgentStatus.DONE:
                break

            contents.append(types.Content(role="user", parts=function_response_parts))

        # Turn loop finished
        self.state.status = AgentStatus.DONE
        self.state.telemetry.wall_clock_seconds = round(time.time() - start_time, 2)

        # 1. Assemble MarketMetrics
        ticker = self.state.ticker or "TCS.NS"
        market_metrics = assemble_market_metrics(ticker, self.state.market_data)

        # 2. Extract structured SentimentFindings if searches occurred
        if not self.state.sentiment_findings:
            self.state.sentiment_findings = _extract_sentiment_from_search(
                client,
                self.search_records,
                self.search_queries_used,
            )
            self.state.telemetry.gemini_calls += 1

        # 3. Chief Editor synthesis
        logger.info("Invoking Chief Editor synthesis...")
        _pace_gemini_call()
        markdown_body = run_chief_editor(
            market_metrics=market_metrics,
            sentiment_findings=self.state.sentiment_findings,
            report_type=self.state.report_type,
            report_spec=self.state.report_spec,
            editorial_goal=self.state.editorial_goal,
            aml_result=self.state.aml_result if self.state.run_aml else None,
        )
        self.state.telemetry.gemini_calls += 1

        # 4. Append AML table if AML was run
        if self.state.run_aml and self.state.aml_result:
            aml_md = render_aml_markdown(self.state.aml_result)
            markdown_body = markdown_body + "\n\n" + aml_md

        # 5. Assemble KPI cards
        kpi_cards: list[dict[str, str]] = []
        if market_metrics.current_price_formatted:
            kpi_cards.append({"label": "Current Price", "value": market_metrics.current_price_formatted, "note": "Market close"})
        if market_metrics.market_cap_formatted:
            kpi_cards.append({"label": "Market Cap", "value": market_metrics.market_cap_formatted, "note": "Scale"})
        if market_metrics.pe_ratio_formatted:
            kpi_cards.append({"label": "P/E Ratio", "value": market_metrics.pe_ratio_formatted, "note": "TTM multiple"})
        if market_metrics.roe_formatted:
            kpi_cards.append({"label": "Return on Equity", "value": market_metrics.roe_formatted, "note": "Profitability"})
        for cm_name, cm_val in self.state.custom_metrics.items():
            if isinstance(cm_val, dict) and cm_val.get("formatted_value") and cm_val.get("status") == "ok":
                kpi_cards.append({
                    "label": cm_name.replace("_", " ").title(),
                    "value": str(cm_val["formatted_value"]),
                    "note": "Custom Metric",
                })

        final_report = FinalReport(
            ticker=ticker,
            company_name=self.state.company_name or market_metrics.company_name,
            report_type=self.state.report_type,
            editorial_goal=self.state.editorial_goal,
            markdown_body=markdown_body,
            market_metrics=market_metrics,
            sentiment_findings=self.state.sentiment_findings,
            aml_result=self.state.aml_result,
            report_spec=self.state.report_spec,
            telemetry=self.state.telemetry,
            kpi_cards=kpi_cards[:6],
        )

        self._dump_trace()
        return self.state, final_report

    def _dump_trace(self) -> None:
        """Write execution trace to outputs/TICKER_DATE_trace.json."""
        try:
            settings.output_dir.mkdir(parents=True, exist_ok=True)
            ticker_slug = (self.state.ticker or "UNRESOLVED").replace(".", "_").replace("/", "_")
            date_slug = date.today().isoformat()
            trace_path = settings.output_dir / f"{ticker_slug}_{date_slug}_trace.json"

            trace_data = {
                "user_query": self.state.user_query,
                "ticker": self.state.ticker,
                "company_name": self.state.company_name,
                "report_type": self.state.report_type.value,
                "editorial_goal": self.state.editorial_goal,
                "run_aml": self.state.run_aml,
                "status": self.state.status.value,
                "turn": self.state.turn,
                "telemetry": self.state.telemetry.model_dump(),
                "report_spec": self.state.report_spec.model_dump() if self.state.report_spec else None,
                "custom_metrics": self.state.custom_metrics,
                "tool_log": [t.model_dump() for t in self.state.tool_log],
            }
            trace_path.write_text(json.dumps(trace_data, indent=2), encoding="utf-8")
            logger.info("Trace file written to %s", trace_path)
        except Exception as exc:
            logger.warning("Could not write trace file: %s", exc)


def run_dsh_orchestrator(
    user_query: str,
    initial_company_ref: Optional[str] = None,
    report_type: ReportType = ReportType.GENERAL,
    run_aml: bool = False,
    editorial_goal: Optional[str] = None,
    interactive_fn: Optional[Callable[[str, list[str]], str]] = None,
) -> tuple[AgentState, FinalReport]:
    """Entry point for running the DSH orchestrator."""
    orchestrator = DSHOrchestrator(
        user_query=user_query,
        initial_company_ref=initial_company_ref,
        report_type=report_type,
        run_aml=run_aml,
        editorial_goal=editorial_goal,
        interactive_fn=interactive_fn,
    )
    return orchestrator.run()
