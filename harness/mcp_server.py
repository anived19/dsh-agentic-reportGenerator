"""
Stateful MCP (Model Context Protocol) tool server for the Financial Report Generator.

Exposes the full agentic tool surface (all 16 tools: data tools, AML tools, search,
custom AST metrics, validation, reflection, report format planning, ask_user, and finalization)
over stdio JSON-RPC transport to the DeepSeek Harness (DSH) runtime.

Maintains session state (AgentState, retry counts, Tavily budget, tool logs) across
the lifetime of the DSH run and persists snapshots to cache/sessions/{session_id}/.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any, Optional

import yaml
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from config import settings
from schemas import (
    AgentState,
    AgentStatus,
    AMLFinding,
    AMLScreeningResult,
    AMLSeverity,
    CitedClaim,
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

logger = logging.getLogger("mcp_server")

# ---------------------------------------------------------------------------
# Session State Manager (Singleton per MCP Server Subprocess)
# ---------------------------------------------------------------------------

class SessionStateManager:
    """Manages mutable session state for a single DSH report generation run."""

    def __init__(self, session_id: Optional[str] = None):
        resolved_id = session_id or os.environ.get("FINOSCALE_SESSION_ID") or os.environ.get("DSH_SESSION_ID")
        if not resolved_id:
            active_file = settings.cache_dir / "sessions" / "active_session.json"
            if active_file.exists():
                try:
                    data = json.loads(active_file.read_text(encoding="utf-8"))
                    resolved_id = data.get("active_session_id")
                except Exception:
                    pass
        self.session_id = resolved_id or f"session_{int(time.time())}"
        self.session_dir = settings.cache_dir / "sessions" / self.session_id
        self.session_dir.mkdir(parents=True, exist_ok=True)

        # Category attempt tracking for validation retry caps
        self.category_attempts: dict[str, int] = {
            "price_snapshot": 0,
            "valuation_multiples": 0,
            "fundamentals": 0,
            "technicals": 0,
            "ownership": 0,
            "quarterly_financials": 0,
            "news_searches": 0,
            "aml_sweep": 0,
        }
        self.search_records: list[dict[str, Any]] = []
        self.search_queries_used: list[str] = []
        self.validation_result: Optional[ValidationResult] = None

        # Load orchestrator config profiles for validation
        self.config_profiles = self._load_orchestrator_config()

        state_file = self.session_dir / "session_state.json"
        init_file = self.session_dir / "session_init.json"

        if state_file.exists():
            self._hydrate_from_checkpoint(state_file)
        elif init_file.exists():
            self._hydrate_from_init(init_file)
        else:
            self.state = self._default_state()

    def _hydrate_from_checkpoint(self, state_file: Path) -> None:
        """Hydrate full session state from persisted checkpoint on subprocess restart."""
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            self.state = AgentState(
                user_query=data.get("user_query", ""),
                company_reference=data.get("company_reference"),
                report_type=ReportType(data.get("report_type", "general")),
                editorial_goal=data.get("editorial_goal", "Standard Comprehensive Financial Analysis"),
                run_aml=data.get("run_aml", False),
                telemetry=RunTelemetry(**data.get("telemetry", {"tavily_calls_budget": 5})),
                status=AgentStatus.RUNNING,
            )
            self.state.ticker = data.get("ticker")
            self.state.company_name = data.get("company_name")
            self.state.turn = data.get("turn", 0)
            self.state.market_data = data.get("market_data", {})
            self.state.custom_metrics = data.get("custom_metrics", {})
            self.state.candidate_entities = data.get("candidate_entities", [])
            if data.get("report_spec"):
                self.state.report_spec = ReportSpec.model_validate(data["report_spec"])
            if data.get("sentiment_findings"):
                self.state.sentiment_findings = SentimentFindings.model_validate(data["sentiment_findings"])
            if data.get("aml_result"):
                self.state.aml_result = AMLScreeningResult.model_validate(data["aml_result"])
            if data.get("validation_result"):
                self.validation_result = ValidationResult.model_validate(data["validation_result"])
            self.state.tool_log = [ToolCallRecord.model_validate(t) for t in data.get("tool_log", [])]
            self.category_attempts.update(data.get("category_attempts", {}))
            logger.info("Hydrated session %s from checkpoint (turn=%s, %d market_data keys)",
                        self.session_id, self.state.turn, len(self.state.market_data))
        except Exception as exc:
            logger.error("Checkpoint hydration failed, falling back to init: %s", exc)
            init_file = self.session_dir / "session_init.json"
            if init_file.exists():
                self._hydrate_from_init(init_file)
            else:
                self.state = self._default_state()

    def _hydrate_from_init(self, init_file: Path) -> None:
        """Initialize AgentState from session_init.json payload."""
        try:
            init_data = json.loads(init_file.read_text(encoding="utf-8"))
            rep_type = ReportType(init_data.get("report_type", "general"))
            self.state = AgentState(
                user_query=init_data.get("user_query", ""),
                company_reference=init_data.get("company_reference"),
                report_type=rep_type,
                editorial_goal=init_data.get("editorial_goal", "Standard Comprehensive Financial Analysis"),
                run_aml=init_data.get("run_aml", False),
                telemetry=RunTelemetry(tavily_calls_budget=5),
                status=AgentStatus.RUNNING,
            )
            if init_data.get("ticker"):
                self.state.ticker = init_data.get("ticker")
            if init_data.get("company_name"):
                self.state.company_name = init_data.get("company_name")
        except Exception as exc:
            logger.warning("Could not load session_init.json: %s", exc)
            self.state = self._default_state()

    def _default_state(self) -> AgentState:
        return AgentState(
            user_query="",
            report_type=ReportType.GENERAL,
            editorial_goal="Standard Comprehensive Financial Analysis",
            run_aml=False,
            telemetry=RunTelemetry(tavily_calls_budget=5),
            status=AgentStatus.RUNNING,
        )

    def _load_orchestrator_config(self) -> dict[str, Any]:
        cfg_path = Path("orchestrator_config.yaml")
        if cfg_path.exists():
            try:
                with open(cfg_path, encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}
            except Exception as exc:
                logger.warning("Failed to load orchestrator_config.yaml: %s", exc)
        return {}

    def checkpoint(self) -> None:
        """Persist atomic snapshot of current session state to disk."""
        try:
            state_file = self.session_dir / "session_state.json"
            data = {
                "session_id": self.session_id,
                "ticker": self.state.ticker,
                "company_name": self.state.company_name,
                "report_type": self.state.report_type.value,
                "editorial_goal": self.state.editorial_goal,
                "run_aml": self.state.run_aml,
                "status": self.state.status.value,
                "turn": self.state.turn,
                "market_data": self.state.market_data,
                "custom_metrics": self.state.custom_metrics,
                "candidate_entities": self.state.candidate_entities,
                "telemetry": self.state.telemetry.model_dump(),
                "report_spec": self.state.report_spec.model_dump() if self.state.report_spec else None,
                "sentiment_findings": self.state.sentiment_findings.model_dump() if self.state.sentiment_findings else None,
                "aml_result": self.state.aml_result.model_dump() if self.state.aml_result else None,
                "validation_result": self.validation_result.model_dump() if hasattr(self, "validation_result") and self.validation_result else None,
                "tool_log": [t.model_dump() for t in self.state.tool_log],
                "category_attempts": self.category_attempts,
            }
            state_file.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to write session checkpoint: %s", exc)

    def dump_final_session(self) -> Path:
        """Save the final session payload for driver consumption."""
        self.checkpoint()
        final_file = self.session_dir / "final_session.json"
        state_file = self.session_dir / "session_state.json"
        if state_file.exists():
            final_file.write_text(state_file.read_text(encoding="utf-8"), encoding="utf-8")
        return final_file


# Global session manager instance for this server process
session_mgr = SessionStateManager()


# ---------------------------------------------------------------------------
# MCP Server Instance & Tool Catalog
# ---------------------------------------------------------------------------

server = Server("finoscale-report-tools")

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "resolve_entity",
        "description": (
            "Resolve a natural-language company or conglomerate reference to candidate ticker symbols. "
            "Returns a list of candidate dicts with ticker, name, exchange, sector, confidence. "
            "If more than one candidate is returned, you MUST immediately call ask_user."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Company name, ticker, or conglomerate reference (e.g. 'Tata', 'TCS', 'Reliance').",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "ask_user",
        "description": (
            "Pauses execution to ask the user to choose when resolve_entity returns >1 candidate entity. "
            "Presents the options to the user and waits for their selection."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The disambiguation prompt to present to the user.",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of candidate company/ticker options.",
                },
            },
            "required": ["question", "options"],
        },
    },
    {
        "name": "get_price_snapshot",
        "description": (
            "Fetch current price, market cap, 50d/200d moving averages, and period high/low. "
            "Returns structured numeric data from yfinance — never hallucinated."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol (e.g. 'TCS.NS', 'AAPL')."}
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_valuation_multiples",
        "description": (
            "Fetch valuation multiples: P/E, forward P/E, P/B, P/S, EV/EBITDA, dividend yield, revenue TTM, and margins."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol."}
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_fundamentals",
        "description": (
            "Fetch EPS (TTM), debt-to-equity, ROE, ROCE, and broker analyst consensus ratings (buy/hold/sell counts, target mean)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol."}
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_quarterly_financials",
        "description": (
            "Fetch historical quarterly revenue, net income, operating margin, and QoQ growth series."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol."}
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_technicals",
        "description": (
            "Fetch technical indicators: RSI-14, MACD (line, signal, hist), 20-day average volume, volume trend, support/resistance."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol."}
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_ownership",
        "description": (
            "Fetch shareholder ownership breakdown: promoter/insider %, institutional holding %, public float %."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol."}
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "compute_custom_financial_metric",
        "description": (
            "Safely evaluate an ad-hoc financial formula (e.g. CAGR, FCF Yield, custom spreads) in a hardened AST sandbox. "
            "Supported functions: cagr, log, sqrt, abs, min, max, round, pow."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "Mathematical formula to evaluate (e.g. 'cagr(start_val, end_val, years)').",
                },
                "ticker": {"type": "string", "description": "Ticker symbol context."},
                "metric_name": {"type": "string", "description": "Name for the computed metric."},
                "context": {
                    "type": "object",
                    "description": "Variable bindings for the formula (e.g. {'start_val': 100, 'end_val': 150, 'years': 3}).",
                },
            },
            "required": ["expression", "ticker", "metric_name"],
        },
    },
    {
        "name": "search_web_news",
        "description": (
            "Search live financial news and market commentary for the entity. "
            "Counts against the shared 5-call Tavily budget per run."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query string."},
                "ticker": {"type": "string", "description": "Ticker symbol context.", "default": ""},
                "depth": {"type": "string", "enum": ["basic", "advanced"], "default": "basic"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "run_structured_aml_sweep",
        "description": (
            "Sweep 8 global sanctions and regulatory databases: OFAC SDN, UN Consolidated List, "
            "EU Financial Sanctions, World Bank Debarment, OpenSanctions PEP, SEC EDGAR FCPA, "
            "TI CPI jurisdictional risk, and FATF monitoring list."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity_name": {"type": "string", "description": "Company name to screen."},
                "ticker": {"type": "string", "description": "Ticker symbol.", "default": ""},
            },
            "required": ["entity_name"],
        },
    },
    {
        "name": "search_adverse_media",
        "description": (
            "Search regulatory enforcement actions, SEBI orders, ED raids, bribery investigations. "
            "Counts against the shared Tavily budget."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity_name": {"type": "string", "description": "Company or individual name."},
                "focus": {"type": "string", "description": "Specific investigation or topic focus.", "default": ""},
                "depth": {"type": "string", "enum": ["basic", "advanced"], "default": "basic"},
            },
            "required": ["entity_name"],
        },
    },
    {
        "name": "validate_data",
        "description": (
            "Evaluate data sufficiency and completeness against the report type requirement profile. "
            "MUST be called and satisfied before finalize_report."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "plan_report_format",
        "description": (
            "Submit a dynamic ReportSpec tailoring report section order and emphasis to findings and editorial goal (max 5-7 sections)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "rationale": {
                    "type": "string",
                    "description": "Explanation of how section ordering and emphasis serve the editorial goal.",
                },
                "sections": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "include": {"type": "boolean"},
                            "order": {"type": "integer"},
                            "custom_instruction": {"type": "string"},
                            "emphasis": {"type": "string"},
                        },
                        "required": ["name", "include", "order"],
                    },
                    "description": "Ordered list of report sections.",
                },
            },
            "required": ["rationale", "sections"],
        },
    },
    {
        "name": "reflect_on_progress",
        "description": (
            "Record a structured reflection checkpoint: summary of gathered data, what is still missing, and why next action follows. "
            "Required at least once before finalize_report."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "gathered_summary": {"type": "string", "description": "Summary of data collected so far."},
                "still_needed": {"type": "string", "description": "Data categories or searches still needed."},
                "next_action_rationale": {"type": "string", "description": "Why the upcoming action serves the editorial goal."},
            },
            "required": ["gathered_summary", "still_needed", "next_action_rationale"],
        },
    },
    {
        "name": "finalize_report",
        "description": (
            "Signal completion of the data gathering and planning phase. "
            "Refuses completion if validate_data requirements are unsatisfied."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]


# ---------------------------------------------------------------------------
# Tool Execution & Dispatching Logic
# ---------------------------------------------------------------------------

def _dispatch_tool(name: str, arguments: dict[str, Any]) -> Any:
    """Execute a tool, update state, and return structured result."""
    state = session_mgr.state
    state.turn += 1

    if name == "resolve_entity":
        from tools.ticker_resolver import resolve_entity
        query = arguments.get("query", "")
        candidates = resolve_entity(query)
        if len(candidates) == 1:
            state.candidate_entities = []
            state.ticker = candidates[0]["ticker"]
            state.company_name = candidates[0]["name"]
            state.status = AgentStatus.RUNNING
        elif len(candidates) > 1:
            state.candidate_entities = candidates
            state.ticker = None
            state.status = AgentStatus.AWAITING_USER
        else:
            state.candidate_entities = []
            state.status = AgentStatus.RUNNING
        session_mgr.checkpoint()
        return {
            "query": query,
            "candidate_count": len(candidates),
            "candidates": candidates,
            "action_required": "call ask_user immediately" if len(candidates) > 1 else "proceed with data fetching",
        }

    elif name == "ask_user":
        question = arguments.get("question", "")
        options = arguments.get("options", [])
        if not options and state.candidate_entities:
            options = [f"{c['name']} ({c['ticker']})" for c in state.candidate_entities]

        # File-based IPC Rendezvous: write pending request
        pending_file = session_mgr.session_dir / "ask_user_pending.json"
        response_file = session_mgr.session_dir / "ask_user_response.json"
        
        # Clean previous response if any
        if response_file.exists():
            response_file.unlink()

        pending_data = {
            "question": question,
            "options": options,
            "timestamp": time.time(),
        }
        pending_file.write_text(json.dumps(pending_data, indent=2), encoding="utf-8")
        logger.info("ask_user IPC request written to %s. Waiting for response...", pending_file)

        # Wait for parent driver to write response
        selected = ""
        wait_seconds = 0
        max_wait = 120  # 2 minute timeout
        while wait_seconds < max_wait:
            if response_file.exists():
                try:
                    resp_data = json.loads(response_file.read_text(encoding="utf-8"))
                    selected = resp_data.get("selected", "")
                    if selected:
                        break
                except Exception:
                    pass
            time.sleep(0.2)
            wait_seconds += 0.2

        # Clean up IPC files
        if pending_file.exists():
            pending_file.unlink()
        if response_file.exists():
            response_file.unlink()

        if not selected and options:
            selected = options[0]
            logger.warning("ask_user timed out; defaulting to %s", selected)

        # Match selected entity
        matched_candidate = None
        ticker_match = re.search(r"\(([A-Za-z0-9\.\^=-]+)\)", selected)
        extracted_sym = ticker_match.group(1).strip() if ticker_match else None

        for c in state.candidate_entities:
            if extracted_sym and c["ticker"].lower() == extracted_sym.lower():
                matched_candidate = c
                break
            if c["ticker"].lower() in selected.lower() or c["name"].lower() in selected.lower():
                matched_candidate = c
                break

        if matched_candidate:
            state.ticker = matched_candidate["ticker"]
            state.company_name = matched_candidate["name"]
        elif extracted_sym:
            state.ticker = extracted_sym
            state.company_name = selected.split("(")[0].strip() or extracted_sym
        elif selected.strip():
            # If user typed a custom ticker/name
            from tools.ticker_resolver import resolve_entity
            typed_candidates = resolve_entity(selected.strip())
            if typed_candidates:
                state.ticker = typed_candidates[0]["ticker"]
                state.company_name = typed_candidates[0]["name"]
            else:
                state.ticker = selected.strip()
                state.company_name = selected.strip()
        elif state.candidate_entities:
            state.ticker = state.candidate_entities[0]["ticker"]
            state.company_name = state.candidate_entities[0]["name"]

        # Reset candidate entities & status to active running state
        state.candidate_entities = []
        state.status = AgentStatus.RUNNING
        session_mgr.checkpoint()
        return {
            "selected": selected,
            "resolved_ticker": state.ticker,
            "resolved_company_name": state.company_name,
        }

    # Helper guard for all data-fetching tools
    if name in (
        "get_price_snapshot", "get_valuation_multiples", "get_fundamentals",
        "get_quarterly_financials", "get_technicals", "get_ownership",
        "compute_custom_financial_metric"
    ):
        if state.status == AgentStatus.AWAITING_USER or (len(state.candidate_entities) > 1 and not state.ticker):
            return {
                "error": "Entity disambiguation required: multiple candidates exist. You MUST call ask_user before fetching market data.",
                "candidates": state.candidate_entities,
            }

    if name == "get_price_snapshot":
        from tools.finance_tools import get_price_snapshot
        ticker = arguments.get("ticker") or state.ticker
        if not ticker:
            return {"error": "No ticker specified or resolved. Call resolve_entity first."}
        session_mgr.category_attempts["price_snapshot"] += 1
        res = get_price_snapshot(ticker)
        state.market_data.update(res)
        if not state.company_name and res.get("company_name"):
            state.company_name = res["company_name"]
        session_mgr.checkpoint()
        return res

    elif name == "get_valuation_multiples":
        from tools.finance_tools import get_valuation_multiples
        ticker = arguments.get("ticker") or state.ticker
        if not ticker:
            return {"error": "No ticker specified or resolved. Call resolve_entity first."}
        session_mgr.category_attempts["valuation_multiples"] += 1
        res = get_valuation_multiples(ticker)
        state.market_data.update(res)
        session_mgr.checkpoint()
        return res

    elif name == "get_fundamentals":
        from tools.finance_tools import get_fundamentals
        ticker = arguments.get("ticker") or state.ticker
        if not ticker:
            return {"error": "No ticker specified or resolved. Call resolve_entity first."}
        session_mgr.category_attempts["fundamentals"] += 1
        res = get_fundamentals(ticker)
        state.market_data.update(res)
        session_mgr.checkpoint()
        return res

    elif name == "get_quarterly_financials":
        from tools.finance_tools import get_quarterly_financials
        ticker = arguments.get("ticker") or state.ticker
        if not ticker:
            return {"error": "No ticker specified or resolved. Call resolve_entity first."}
        session_mgr.category_attempts["quarterly_financials"] += 1
        res = get_quarterly_financials(ticker)
        state.market_data["quarterly_financials"] = res
        session_mgr.checkpoint()
        return {"quarterly_datapoints_count": len(res), "data": res}

    elif name == "get_technicals":
        from tools.finance_tools import get_technicals
        ticker = arguments.get("ticker") or state.ticker
        if not ticker:
            return {"error": "No ticker specified or resolved. Call resolve_entity first."}
        session_mgr.category_attempts["technicals"] += 1
        res = get_technicals(ticker)
        state.market_data.update(res)
        session_mgr.checkpoint()
        return res

    elif name == "get_ownership":
        from tools.finance_tools import get_ownership
        ticker = arguments.get("ticker") or state.ticker
        if not ticker:
            return {"error": "No ticker specified or resolved. Call resolve_entity first."}
        session_mgr.category_attempts["ownership"] += 1
        res = get_ownership(ticker)
        state.market_data.update(res)
        session_mgr.checkpoint()
        return res

    elif name == "compute_custom_financial_metric":
        from tools.finance_tools import compute_custom_financial_metric
        ticker = arguments.get("ticker") or state.ticker
        res = compute_custom_financial_metric(
            expression=arguments.get("expression", ""),
            ticker=ticker,
            metric_name=arguments.get("metric_name", "custom_metric"),
            context=arguments.get("context", {}),
        )
        metric_name = arguments.get("metric_name", "custom_metric")
        state.custom_metrics[metric_name] = res
        session_mgr.checkpoint()
        return res

    elif name == "search_web_news":
        from tools.search_tools import search_web_news
        query = arguments.get("query", "")
        ticker = arguments.get("ticker") or state.ticker or ""
        depth = arguments.get("depth", "basic")

        if state.telemetry.tavily_calls >= state.telemetry.tavily_calls_budget:
            return {"warning": "Tavily search budget exhausted (5/5). No additional queries permitted."}

        session_mgr.category_attempts["news_searches"] += 1
        state.telemetry.tavily_calls += 1
        res = search_web_news(query=query, ticker=ticker, depth=depth)

        session_mgr.search_queries_used.append(query)
        session_mgr.search_records.append(res)

        # Populate basic sentiment findings from news claims
        catalysts: list[CitedClaim] = []
        risks: list[CitedClaim] = []
        if isinstance(res, dict) and "articles" in res:
            for art in res.get("articles", [])[:3]:
                t = art.get("title", "")
                u = art.get("url", "")
                if t and u:
                    catalysts.append(CitedClaim(claim=t, source_url=u))

        if not state.sentiment_findings:
            state.sentiment_findings = SentimentFindings(
                overall_sentiment=SentimentLabel.NEUTRAL,
                sentiment_summary=f"Recent market news for {state.company_name or ticker}.",
                key_catalysts=catalysts,
                key_risks=risks,
            )
        else:
            state.sentiment_findings.key_catalysts.extend(catalysts)

        session_mgr.checkpoint()
        return res

    elif name == "run_structured_aml_sweep":
        from tools.aml_tools import run_structured_aml_sweep
        entity_name = arguments.get("entity_name") or state.company_name or "Tata Consultancy Services"
        ticker = arguments.get("ticker") or state.ticker or ""

        session_mgr.category_attempts["aml_sweep"] += 1
        res = run_structured_aml_sweep(entity_name=entity_name, ticker=ticker)

        raw_findings = res if isinstance(res, list) else res.get("findings", [])
        findings = [AMLFinding(**f) if isinstance(f, dict) else f for f in raw_findings]

        screened_entities = [entity_name]
        if ticker and ticker != entity_name:
            screened_entities.append(ticker)

        state.aml_result = AMLScreeningResult(
            entities_screened=screened_entities,
            findings=findings,
        )
        session_mgr.checkpoint()
        return res

    elif name == "search_adverse_media":
        from tools.aml_tools import search_adverse_media
        entity_name = arguments.get("entity_name") or state.company_name or ""
        focus = arguments.get("focus", "")
        depth = arguments.get("depth", "basic")

        if state.telemetry.tavily_calls >= state.telemetry.tavily_calls_budget:
            return {"warning": "Tavily search budget exhausted (5/5)."}

        state.telemetry.tavily_calls += 1
        res = search_adverse_media(entity_name=entity_name, focus=focus, depth=depth)
        session_mgr.checkpoint()
        return res

    elif name == "validate_data":
        # Check completeness against orchestrator_config.yaml profile
        rep_type_key = state.report_type.value.upper()
        profile = session_mgr.config_profiles.get(rep_type_key, session_mgr.config_profiles.get("GENERAL", {}))
        required_cats = profile.get("required", ["price_snapshot"])
        optional_cats = profile.get("optional", [])
        min_news = profile.get("min_news_searches", 1)

        cat_mapping = {
            "price_snapshot": "current_price" in state.market_data,
            "valuation_multiples": "pe_ratio" in state.market_data,
            "fundamentals": "eps_ttm" in state.market_data or "debt_to_equity" in state.market_data,
            "technicals": "rsi_14" in state.market_data,
            "ownership": "promoter_holding_pct" in state.market_data,
            "quarterly_financials": bool(state.market_data.get("quarterly_financials")),
        }

        missing_required: list[str] = []
        retriable_missing: list[str] = []
        for cat in required_cats:
            has_data = cat_mapping.get(cat, False)
            if not has_data:
                attempts = session_mgr.category_attempts.get(cat, 0)
                if attempts < 2:
                    retriable_missing.append(cat)
                missing_required.append(cat)

        news_count = session_mgr.category_attempts.get("news_searches", 0)
        news_satisfied = news_count >= min_news

        missing_for_report = list(retriable_missing)
        if not news_satisfied:
            missing_for_report.append(f"news_searches (ran {news_count}, need {min_news})")

        # Satisfied if all required are present OR capped out on retries
        satisfied = (len(retriable_missing) == 0) and news_satisfied

        val_result = ValidationResult(
            satisfied=satisfied,
            missing=missing_for_report,
            contradictions=[],
            notes=(
                "Proceed to plan_report_format and finalize_report."
                if satisfied
                else f"Missing: {missing_for_report}. Call search_web_news or the relevant get_* data fetch tool."
            ),
        )
        session_mgr.validation_result = val_result
        session_mgr.checkpoint()
        return val_result.model_dump()

    elif name == "plan_report_format":
        rationale = arguments.get("rationale", "")
        raw_sections = arguments.get("sections", [])
        sec_specs = []
        for s in raw_sections:
            sec_name = s.get("name") or s.get("title") or "Section"
            sec_key = s.get("key") or sec_name.lower().replace(" ", "_")
            sec_specs.append(SectionSpec(
                key=sec_key,
                title=s.get("title") or sec_name,
                include=s.get("include", True),
                order=s.get("order", len(sec_specs) + 1),
                instruction=s.get("custom_instruction") or s.get("instruction"),
                emphasis=s.get("emphasis", ""),
            ))
        state.report_spec = ReportSpec(
            sections=sec_specs,
            rationale=rationale,
            editorial_goal=state.editorial_goal,
            report_spec_source="dsh_agentic_plan",
        )
        session_mgr.checkpoint()
        return {
            "status": "accepted",
            "section_count": len(sec_specs),
            "rationale": rationale,
        }

    elif name == "reflect_on_progress":
        record = ToolCallRecord(
            turn=state.turn,
            tool_name="reflect_on_progress",
            arguments=arguments,
            result_summary="Reflection checkpoint recorded.",
            ok=True,
            reasoning_text=arguments.get("next_action_rationale"),
        )
        state.tool_log.append(record)
        session_mgr.checkpoint()
        return {"status": "checkpoint_recorded"}

    elif name == "finalize_report":
        # Verify validation
        if not session_mgr.validation_result or not session_mgr.validation_result.satisfied:
            # Auto-validate once to check
            val_dict = _dispatch_tool("validate_data", {})
            if not val_dict.get("satisfied"):
                return {
                    "error": "Cannot finalize report: required data validation unsatisfied.",
                    "validation": val_dict,
                }

        state.status = AgentStatus.DONE
        final_path = session_mgr.dump_final_session()
        return {
            "status": "finalized",
            "session_id": session_mgr.session_id,
            "final_payload_path": str(final_path),
            "ticker": state.ticker,
            "company_name": state.company_name,
        }

    else:
        raise ValueError(f"Unknown MCP tool: {name}")


# ---------------------------------------------------------------------------
# MCP Handlers
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[Tool]:
    """Return the complete 16-tool catalog to DSH."""
    return [
        Tool(
            name=t["name"],
            description=t["description"],
            inputSchema=t["inputSchema"],
        )
        for t in TOOL_DEFINITIONS
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent]:
    """Execute tool, record in tool log, and return JSON result."""
    arguments = arguments or {}
    logger.info("MCP call: %s(%s)", name, arguments)

    rec = ToolCallRecord(
        turn=session_mgr.state.turn,
        tool_name=name,
        arguments=arguments,
        result_summary="",
        ok=True,
    )

    try:
        result = _dispatch_tool(name, arguments)
        rec.result_summary = str(result)[:200]
        session_mgr.state.tool_log.append(rec)
        session_mgr.checkpoint()
        return [TextContent(type="text", text=json.dumps(result, default=str, ensure_ascii=False))]
    except Exception as exc:
        logger.exception("MCP tool %s failed", name)
        rec.ok = False
        rec.error = str(exc)
        session_mgr.state.tool_log.append(rec)
        session_mgr.checkpoint()
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}, ensure_ascii=False))]


# ---------------------------------------------------------------------------
# Stdio Server Entry Point
# ---------------------------------------------------------------------------

async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logger.info("Starting Stateful Finoscale MCP tool server for session %s...", session_mgr.session_id)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
