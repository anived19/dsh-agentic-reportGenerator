"""
Stateful MCP (Model Context Protocol) tool server for the Financial Report Generator.

Exposes the full agentic tool surface (all 16 tools: data tools, AML tools, search,
custom AST metrics, validation, reflection, report format planning, ask_user, and finalization)
over stdio JSON-RPC transport to the DeepSeek Harness (DSH) runtime.

Maintains session state (AgentState, retry counts, Tavily budget, tool logs) across
the lifetime of the DSH run and persists snapshots to cache/sessions/{session_id}/.
"""
from __future__ import annotations

import os
import asyncio
import json
import logging
import re
import sys
import time
import threading
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
        self.active_subagent_category: Optional[str] = None
        self.bounded_index: dict[str, list[int]] = {}
        self.parsed_pages: list[dict] = []
        self.fallback_dossier: dict[str, str] = {}

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
            self.state.completed_tools = data.get("completed_tools", [])
            self.state.credit_scoring_attempted = data.get("credit_scoring_attempted", False)

            if data.get("score_results"):
                from schemas import ScoreCategoryResult
                try:
                    self.state.score_results = [
                        ScoreCategoryResult.model_validate(r) for r in data["score_results"]
                    ]
                except Exception as exc:
                    logger.warning("Failed to hydrate score_results for session %s: %s", self.session_id, exc)

            if data.get("analyst_review_status"):
                from schemas import AnalystReviewStatus
                try:
                    self.state.analyst_review_status = AnalystReviewStatus(data["analyst_review_status"])
                except Exception:
                    pass

            # NOTE: deliberately NOT restoring active_subagent_category from disk.
            # It lives on SessionStateManager (not AgentState) as an in-process lock.
            # If a crash happened while a subagent category was locked but never
            # submitted, the fresh DSH conversation on resume has zero memory of
            # ever calling get_category_text for it — so restoring the lock would
            # permanently block that category (nothing could ever call
            # submit_category_result to release it). Always start unlocked; the
            # category-completeness guard in get_category_text (Bug 3 fix) is what
            # prevents re-scoring anything that actually finished.
            self.active_subagent_category = None

            if data.get("report_spec"):
                self.state.report_spec = ReportSpec.model_validate(data["report_spec"])
            if data.get("sentiment_findings"):
                self.state.sentiment_findings = SentimentFindings.model_validate(data["sentiment_findings"])
            if data.get("aml_result"):
                self.state.aml_result = AMLScreeningResult.model_validate(data["aml_result"])
            if data.get("sector_metrics"):
                from schemas import SectorMetricsContainer
                self.state.sector_metrics = SectorMetricsContainer.model_validate(data["sector_metrics"])
            if data.get("peer_benchmarks"):
                from schemas import PeerBenchmarkData
                self.state.peer_benchmarks = PeerBenchmarkData.model_validate(data["peer_benchmarks"])
            if data.get("anomaly_findings"):
                from schemas import AnomalyInvestigationFinding
                self.state.anomaly_findings = [AnomalyInvestigationFinding.model_validate(f) for f in data["anomaly_findings"]]
            if data.get("cro_audit_report"):
                from schemas import CROAuditReport
                self.state.cro_audit_report = CROAuditReport.model_validate(data["cro_audit_report"])
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
                "user_query": self.state.user_query,
                "company_reference": self.state.company_reference,
                "ticker": self.state.ticker,
                "company_name": self.state.company_name,
                "report_type": self.state.report_type.value,
                "editorial_goal": self.state.editorial_goal,
                "run_aml": self.state.run_aml,
                "credit_scoring_attempted": self.state.credit_scoring_attempted,
                "completed_tools": self.state.completed_tools,
                "status": self.state.status.value,
                "turn": self.state.turn,
                "market_data": self.state.market_data,
                "custom_metrics": self.state.custom_metrics,
                "sector_metrics": self.state.sector_metrics.model_dump() if self.state.sector_metrics else None,
                "peer_benchmarks": self.state.peer_benchmarks.model_dump() if self.state.peer_benchmarks else None,
                "anomaly_findings": [f.model_dump() for f in self.state.anomaly_findings],
                "cro_audit_report": self.state.cro_audit_report.model_dump() if self.state.cro_audit_report else None,
                "candidate_entities": self.state.candidate_entities,
                "telemetry": self.state.telemetry.model_dump(),
                "report_spec": self.state.report_spec.model_dump() if self.state.report_spec else None,
                "sentiment_findings": self.state.sentiment_findings.model_dump() if self.state.sentiment_findings else None,
                "aml_result": self.state.aml_result.model_dump() if self.state.aml_result else None,
                "validation_result": self.validation_result.model_dump() if hasattr(self, "validation_result") and self.validation_result else None,
                "tool_log": [t.model_dump() for t in self.state.tool_log],
                "score_results": [r.model_dump() for r in self.state.score_results],
                "analyst_review_status": self.state.analyst_review_status.value,
                "active_subagent_category": self.active_subagent_category,
                "tool_call_summary": {
                    "total_calls": len(self.state.tool_log),
                    "unique_tools": list(set(t.tool_name for t in self.state.tool_log)),
                    "error_count": sum(1 for t in self.state.tool_log if not t.ok),
                }
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
            "Disambiguation is handled internally and resolved_ticker in the response is already final - no follow-up tool call is needed."
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
            },
            
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
            },
            
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
            },
            
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
            },
            
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
            },
            
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
            },
            
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
                "metric_name": {"type": "string", "description": "Name for the computed metric."},
                "context": {
                    "type": "object",
                    "description": "Variable bindings for the formula (e.g. {'start_val': 100, 'end_val': 150, 'years': 3}).",
                },
            },
            "required": ["expression", "metric_name"],
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
        "name": "compute_banking_metrics",
        "description": (
            "Deterministic banking & financial institution metrics calculator. "
            "Computes Net Interest Margin (NIM) proxy, Efficiency Ratio, Return on Assets (ROA), "
            "Equity-to-Assets ratio, and Loan-to-Deposit proxy."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
            },
            
        },
    },
    {
        "name": "compute_saas_metrics",
        "description": (
            "Deterministic SaaS & Technology metrics calculator. "
            "Computes Rule of 40 score (YoY Rev Growth + FCF Margin), ARR Run-Rate, "
            "Free Cash Flow Margin, and Revenue per Employee."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
            },
            
        },
    },
    {
        "name": "compute_retail_consumer_metrics",
        "description": (
            "Deterministic Retail & Consumer Goods metrics calculator. "
            "Computes Inventory Turnover, Days Sales of Inventory (DSI), Asset Turnover, "
            "and Gross Margin Stability."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
            },
            
        },
    },
    {
        "name": "get_peer_tickers",
        "description": (
            "Discover 3-5 validated industry peers and competitors for a given ticker symbol. "
            "Returns structured peer metadata including name, market cap, and valuation multiples."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_peers": {"type": "integer", "description": "Maximum number of peers to return (default 4)."},
            },
            
        },
    },
    {
        "name": "investigate_financial_anomaly",
        "description": (
            "Contextual deep-dive anomaly hunter ('The Why Loop'). "
            "Issues targeted web searches to explain sharp QoQ profit drops, margin contractions, "
            "or debt spikes before report synthesis."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "anomaly_type": {"type": "string", "description": "Type of anomaly (e.g. 'QoQ Net Income Plunge', 'Debt Surge')."},
                "metric_impacted": {"type": "string", "description": "Specific metric impacted."},
                "observed_value": {"type": "string", "description": "Observed value in latest quarter."},
                "prior_value": {"type": "string", "description": "Prior or baseline value."},
                "query_hint": {"type": "string", "description": "Optional search hint."},
            },
            "required": ["anomaly_type"],
        },
    },
    {
        "name": "audit_draft",
        "description": (
            "Chief Risk Officer (CRO) deterministic self-audit verification tool. "
            "Strictly cross-checks proposed draft claims and numbers against ground-truth market data prior to finalization."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "draft_summary": {"type": "string", "description": "Summary or draft content to audit."},
                "cross_check_items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "metric_name": {"type": "string"},
                            "stated_value": {"type": "string"},
                        },
                    },
                    "description": "Specific key metrics to cross-verify against ground truth.",
                },
            },
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
    {
        "name": "compute_dynamic_scores",
        "description": (
            "Normalize dynamic scores for missing data. Enforces edge cases such as unrated, no banking facilities, "
            "and clean adverse media with specific exact strings required by synthesis."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "The scoring category."},
                "activeFBLimits": {"type": "integer", "default": 0},
                "activeNFBLimits": {"type": "integer", "default": 0},
                "agencyRating": {"type": "string"},
                "adverseMediaFound": {"type": "boolean", "default": False},
                "entityType": {"type": "string", "default": "Corporate"}
            }
        }
    },
    {
        "name": "scrape_url",
        "description": (
            "Universal web scraper capable of fetching and parsing ANY website or API endpoint. "
            "Extracts clean markdown text, structured tables, and metadata. "
            "Supports fast-path HTTP/2 with Playwright headless browser fallback."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Target web URL (HTTP or HTTPS)."},
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional specific field names to extract (e.g. ['Revenue', 'Net Profit']).",
                },
                "selector": {"type": "string", "description": "Optional CSS selector to focus extraction on a specific container."},
                "extract_mode": {
                    "type": "string",
                    "enum": ["auto", "text", "tables", "json"],
                    "default": "auto",
                    "description": "Extraction mode: 'auto' (text + tables), 'text' (markdown text only), 'tables' (structured tables only), 'json' (raw JSON).",
                },
                "use_browser": {
                    "type": "boolean",
                    "default": False,
                    "description": "If True, renders page using headless Chromium to execute client-side JavaScript.",
                },
                "max_length": {
                    "type": "integer",
                    "default": 8000,
                    "description": "Maximum character length of extracted text.",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "scrape_moneycontrol",
        "description": (
            "Specialized Moneycontrol.com financial portal scraper for Indian equities. "
            "Extracts key data: 20D Avg Delivery %, VWAP, Beta, 52-Week High/Low, All-Time High/Low, "
            "Book Value, Market Cap (Cr), and financial tables."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional specific fields to extract (e.g. ['beta', 'delivery', 'vwap', '52_week_high']).",
                },
                "section": {
                    "type": "string",
                    "default": "overview",
                    "description": "Financial section ('overview', 'financials', 'ratios', 'peers').",
                },
                "use_browser": {
                    "type": "boolean",
                    "default": False,
                    "description": "If True, uses headless Chromium for dynamic widgets.",
                },
            }
        },
    },
]


# ---------------------------------------------------------------------------
# Tool Execution & Dispatching Logic
# ---------------------------------------------------------------------------

_state_lock = asyncio.Lock()

# Canonical category names for credit scoring — validated at the gate
from schemas import ScoreCategory
_VALID_CATEGORIES = {c.value for c in ScoreCategory}


# ---------------------------------------------------------------------------
# Adaptive sliding-window rate limiter (replaces static asyncio.sleep)
# ---------------------------------------------------------------------------
class _RateLimiter:
    """Sliding-window rate limiter: only sleeps when genuinely near the ceiling."""

    def __init__(self, max_calls: int = 13, window_seconds: float = 60.0):
        from collections import deque
        self._timestamps: deque[float] = deque()
        self._max_calls = max_calls
        self._window = window_seconds

    async def acquire(self):
        now = time.monotonic()
        # Purge expired timestamps outside the sliding window
        while self._timestamps and now - self._timestamps[0] > self._window:
            self._timestamps.popleft()
        if len(self._timestamps) >= self._max_calls:
            sleep_for = self._window - (now - self._timestamps[0]) + 0.5
            logger.info("Rate limiter: sleeping %.1fs to stay under %d calls/min", sleep_for, self._max_calls)
            await asyncio.sleep(sleep_for)
            # Purge again after sleep
            now = time.monotonic()
            while self._timestamps and now - self._timestamps[0] > self._window:
                self._timestamps.popleft()
        self._timestamps.append(time.monotonic())


_rate_limiter = _RateLimiter(max_calls=10, window_seconds=60.0)


async def _dispatch_tool(name: str, arguments: dict[str, Any]) -> Any:
    """Execute a tool, update state, and return structured result."""
    # Restore universal pacing — protects EVERY tool call, not just the subagent
    # boundary. Keep the extra subagent-specific buffer on top of it, since a
    # subagent spawn is DSH-native and invisible to this limiter's call count.
    await _rate_limiter.acquire()
    if name == "get_category_text":
        logger.info("Extra buffer: 12.0s before subagent spawn (opaque internal Gemini usage).")
        await asyncio.sleep(12.0)
    elif name == "submit_category_result":
        logger.info("Extra buffer: 8.0s after subagent returns.")
        await asyncio.sleep(8.0)
    state = session_mgr.state
    state.turn += 1

    # Bug A Fix: Check for analyst_review_response.json
    analyst_response_file = session_mgr.session_dir / "analyst_review_response.json"
    if analyst_response_file.exists():
        try:
            from schemas import AnalystReviewStatus
            data = json.loads(analyst_response_file.read_text(encoding="utf-8"))
            if "status" in data:
                if data["status"] == "approved":
                    state.analyst_review_status = AnalystReviewStatus.APPROVED
                elif data["status"] == "rejected":
                    state.analyst_review_status = AnalystReviewStatus.REJECTED
            # Rename to prevent redundant reading
            os.replace(analyst_response_file, session_mgr.session_dir / "analyst_review_response.json.processed")
        except Exception:
            pass

    IDEMPOTENT_TOOLS = {
        "get_price_snapshot",
        "get_fundamentals",
        "get_quarterly_financials",
        "get_technicals",
        "get_ownership",
        "get_valuation_multiples"
    }

    if name in IDEMPOTENT_TOOLS:
        if name in state.completed_tools:
            return {"status": "success", "cached": True, "message": "Data already fetched in a previous turn and stored in state."}

    if name == "resolve_entity":
        # Short-circuit: prevent LLM from overriding a resolved ticker
        if state.ticker:
            logger.info("resolve_entity short-circuit: LLM attempted to re-resolve, forcing %s", state.ticker)
            return {
                "query": arguments.get("query", ""),
                "resolved_ticker": state.ticker,
                "resolved_company_name": state.company_name,
                "status": "ALREADY_RESOLVED",
                "instruction": "Ticker is locked. Immediately call get_price_snapshot or get_fundamentals next."
            }
    
    if name == "compute_dynamic_scores":
        result = dict(arguments)
        if result.get("activeFBLimits", 0) == 0 and result.get("activeNFBLimits", 0) == 0:
            result["bankingScore"] = None
            result["bankingScoreText"] = "BANKING SCORE: N/A - Entity does not maintain active banking/credit facilities."
        if not result.get("agencyRating"):
            result["creditRatingText"] = "CREDIT RATING: Unrated / No Public Agency Rating Available."
        if not result.get("adverseMediaFound"):
            result["adverseMediaText"] = "Clear Pass: No adverse findings across 60+ regulatory and legal databases."
        if result.get("entityType") in ["LLP", "Partnership"]:
            result["cinText"] = "CIN: N/A (LLP / Partnership Entity)"
            result["mcaChecks"] = "Clear (Not Applicable for Non-Corporate Entities)"
        return result
            }

        from tools.ticker_resolver import resolve_entity
        query = arguments.get("query", "")
        candidates = resolve_entity(query)
        if len(candidates) == 1:
            state.candidate_entities = []
            state.ticker = candidates[0]["ticker"]
            state.company_name = candidates[0]["name"]
            state.status = AgentStatus.RUNNING
        elif len(candidates) > 1:
            # Force IPC pause directly in Python to prevent LLM bypass
            options = [f"{c['name']} ({c['ticker']})" for c in candidates]
            pending_file = session_mgr.session_dir / "ask_user_pending.json"
            response_file = session_mgr.session_dir / "ask_user_response.json"
            if response_file.exists(): response_file.unlink()
            pending_file.write_text(json.dumps({
                "question": f"Multiple entities found for '{query}'. Please select one:",
                "options": options,
                "timestamp": time.time(),
            }, indent=2), encoding="utf-8")
            
            wait_seconds = 0
            selected = ""
            while wait_seconds < 120:
                if response_file.exists():
                    try:
                        resp_data = json.loads(response_file.read_text(encoding="utf-8"))
                        selected = resp_data.get("selected", "")
                        if selected: break
                    except Exception: pass
                await asyncio.sleep(0.2)
                wait_seconds += 0.2
                
            if pending_file.exists(): pending_file.unlink()
            if response_file.exists(): response_file.unlink()
            
            if not selected: selected = options[0]
            
            matched_candidate = _match_candidate(selected, candidates)
            if not matched_candidate:
                matched_candidate = candidates[0]
                
            state.candidate_entities = []
            state.ticker = matched_candidate["ticker"]
            state.company_name = matched_candidate["name"]
            state.status = AgentStatus.RUNNING
            
        else:
            state.candidate_entities = []
            state.status = AgentStatus.RUNNING
        session_mgr.checkpoint()
        return {
            "query": query,
            "resolved_ticker": state.ticker,
            "resolved_company_name": state.company_name,
        }

    elif name == "ask_user":
        if state.ticker and not state.candidate_entities:
            logger.info("ask_user short-circuit: state.ticker already resolved to %s and no candidates pending.", state.ticker)
            return {
                "selected": state.company_name,
                "resolved_ticker": state.ticker,
                "resolved_company_name": state.company_name,
            }

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
            await asyncio.sleep(0.2)
            wait_seconds += 0.2

        # Clean up IPC files
        if pending_file.exists():
            pending_file.unlink()
        if response_file.exists():
            response_file.unlink()

        if not selected and options:
            selected = options[0]
            logger.warning("ask_user timed out; defaulting to %s", selected)

        matched_candidate = _match_candidate(selected, state.candidate_entities)
        
        ticker_match = re.search(r"\(([A-Za-z0-9\.\^=-]+)\)", selected)
        extracted_sym = ticker_match.group(1).strip() if ticker_match else None

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

    if name in (
        "get_price_snapshot", "get_valuation_multiples", "get_fundamentals",
        "get_quarterly_financials", "get_technicals", "get_ownership",
        "compute_custom_financial_metric", "compute_banking_metrics",
        "compute_saas_metrics", "compute_retail_consumer_metrics",
        "get_peer_tickers", "investigate_financial_anomaly",
        "scrape_url", "scrape_moneycontrol", "search_web_news",
        "compare_source_data", "run_structured_aml_sweep", "search_adverse_media",
        "get_promoter_holding"
    ):
        if state.status == AgentStatus.AWAITING_USER or (len(state.candidate_entities) > 1 and not state.ticker):
            return {
                "error": "Entity disambiguation required: multiple candidates exist. You MUST call ask_user before fetching market data.",
                "candidates": state.candidate_entities,
            }

    if name == "get_price_snapshot":
        from tools.finance_tools import get_price_snapshot
        ticker = state.ticker
        if not ticker:
            return {"error": "No ticker specified or resolved. Call resolve_entity first."}
        session_mgr.category_attempts["price_snapshot"] += 1
        res = get_price_snapshot(ticker)
        state.market_data.update(res)
        if not state.company_name and res.get("company_name"):
            state.company_name = res["company_name"]
        state.completed_tools.append(name)
        session_mgr.checkpoint()
        return res

    elif name == "get_valuation_multiples":
        from tools.finance_tools import get_valuation_multiples
        ticker = state.ticker
        if not ticker:
            return {"error": "No ticker specified or resolved. Call resolve_entity first."}
        session_mgr.category_attempts["valuation_multiples"] += 1
        res = get_valuation_multiples(ticker)
        state.market_data.update(res)
        state.completed_tools.append(name)
        session_mgr.checkpoint()
        return res

    elif name == "get_fundamentals":
        from tools.finance_tools import get_fundamentals
        ticker = state.ticker
        if not ticker:
            return {"error": "No ticker specified or resolved. Call resolve_entity first."}
        session_mgr.category_attempts["fundamentals"] += 1
        res = get_fundamentals(ticker)
        state.market_data.update(res)
        state.completed_tools.append(name)
        session_mgr.checkpoint()
        return res

    elif name == "get_quarterly_financials":
        from tools.finance_tools import get_quarterly_financials
        ticker = state.ticker
        if not ticker:
            return {"error": "No ticker specified or resolved. Call resolve_entity first."}
        session_mgr.category_attempts["quarterly_financials"] += 1
        res = get_quarterly_financials(ticker)
        state.market_data["quarterly_financials"] = res
        state.completed_tools.append(name)
        session_mgr.checkpoint()
        return {"quarterly_datapoints_count": len(res), "data": res}

    elif name == "get_technicals":
        from tools.finance_tools import get_technicals
        ticker = state.ticker
        if not ticker:
            return {"error": "No ticker specified or resolved. Call resolve_entity first."}
        session_mgr.category_attempts["technicals"] += 1
        res = get_technicals(ticker)
        state.market_data.update(res)
        state.completed_tools.append(name)
        session_mgr.checkpoint()
        return res

    elif name == "get_ownership":
        from tools.finance_tools import get_ownership
        ticker = state.ticker
        if not ticker:
            return {"error": "No ticker specified or resolved. Call resolve_entity first."}
        session_mgr.category_attempts["ownership"] += 1
        res = get_ownership(ticker)
        state.market_data.update(res)
        state.completed_tools.append(name)
        session_mgr.checkpoint()
        return res

    elif name == "compute_banking_metrics":
        from tools.finance_tools import compute_banking_metrics
        ticker = state.ticker
        if not ticker:
            return {"error": "No ticker specified or resolved. Call resolve_entity first."}
        res = compute_banking_metrics(ticker)
        from schemas import BankingMetrics, SectorMetricsContainer
        if not state.sector_metrics:
            state.sector_metrics = SectorMetricsContainer(sector="Banking & Financials", banking=BankingMetrics(**res))
        else:
            state.sector_metrics.banking = BankingMetrics(**res)
        session_mgr.checkpoint()
        return res

    elif name == "compute_saas_metrics":
        from tools.finance_tools import compute_saas_metrics
        ticker = state.ticker
        if not ticker:
            return {"error": "No ticker specified or resolved. Call resolve_entity first."}
        res = compute_saas_metrics(ticker)
        from schemas import SaaSMetrics, SectorMetricsContainer
        if not state.sector_metrics:
            state.sector_metrics = SectorMetricsContainer(sector="Technology & SaaS", saas=SaaSMetrics(**res))
        else:
            state.sector_metrics.saas = SaaSMetrics(**res)
        session_mgr.checkpoint()
        return res

    elif name == "compute_retail_consumer_metrics":
        from tools.finance_tools import compute_retail_consumer_metrics
        ticker = state.ticker
        if not ticker:
            return {"error": "No ticker specified or resolved. Call resolve_entity first."}
        res = compute_retail_consumer_metrics(ticker)
        from schemas import RetailConsumerMetrics, SectorMetricsContainer
        if not state.sector_metrics:
            state.sector_metrics = SectorMetricsContainer(sector="Retail & Consumer Goods", retail=RetailConsumerMetrics(**res))
        else:
            state.sector_metrics.retail = RetailConsumerMetrics(**res)
        session_mgr.checkpoint()
        return res

    elif name == "get_peer_tickers":
        from tools.peer_resolver import get_peer_tickers
        ticker = state.ticker
        if not ticker:
            return {"error": "No ticker specified or resolved. Call resolve_entity first."}
        max_peers = arguments.get("max_peers", 4)
        res = get_peer_tickers(ticker, max_peers=max_peers)
        from schemas import PeerBenchmarkData, PeerCompanyInfo
        peers_list = [PeerCompanyInfo(**p) for p in res.get("peers", [])]
        state.peer_benchmarks = PeerBenchmarkData(
            target_ticker=res.get("target_ticker", ticker),
            target_name=res.get("target_name", state.company_name),
            industry=res.get("industry"),
            peers=peers_list,
            industry_summary=res.get("industry_summary"),
        )
        session_mgr.checkpoint()
        return res

    elif name == "investigate_financial_anomaly":
        from tools.search_tools import investigate_financial_anomaly
        ticker = state.ticker
        if not ticker:
            return {"error": "No ticker specified or resolved. Call resolve_entity first."}
        anomaly_type = arguments.get("anomaly_type", "Financial Anomaly")
        metric_impacted = arguments.get("metric_impacted", "")
        observed_val = arguments.get("observed_value", "")
        prior_val = arguments.get("prior_value", "")
        query_hint = arguments.get("query_hint", "")

        res = investigate_financial_anomaly(
            company_name=state.company_name or "",
            ticker=ticker,
            anomaly_type=anomaly_type,
            metric_impacted=metric_impacted,
            observed_value=observed_val,
            prior_value=prior_val,
            query_hint=query_hint,
        )
        from schemas import AnomalyInvestigationFinding
        for f in res.get("findings", []):
            state.anomaly_findings.append(AnomalyInvestigationFinding(**f))
        session_mgr.checkpoint()
        return res

    elif name == "audit_draft":
        from tools.finance_tools import audit_draft_metrics
        draft_summary = arguments.get("draft_summary", "")
        cross_check_items = arguments.get("cross_check_items", [])
        res = audit_draft_metrics(
            market_data=state.market_data,
            draft_summary=draft_summary,
            cross_check_items=cross_check_items,
        )
        from schemas import CROAuditReport
        state.cro_audit_report = CROAuditReport(**res)
        session_mgr.checkpoint()
        return res

    elif name == "compute_custom_financial_metric":
        from tools.finance_tools import compute_custom_financial_metric
        ticker = state.ticker
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
        ticker = state.ticker or ""
        depth = arguments.get("depth", "basic")

        session_mgr.category_attempts["news_searches"] += 1
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
        entity_name = state.company_name or arguments.get("entity_name") or ""
        ticker = state.ticker or ""

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
        entity_name = state.company_name or arguments.get("entity_name") or ""
        focus = arguments.get("focus", "")
        depth = arguments.get("depth", "basic")

        res = search_adverse_media(entity_name=entity_name, focus=focus, depth=depth)
        session_mgr.checkpoint()
        return res

    elif name == "scrape_url":
        from tools.scraper_tools import scrape_url
        url = arguments.get("url", "")
        fields = arguments.get("fields")
        selector = arguments.get("selector")
        extract_mode = arguments.get("extract_mode", "auto")
        use_browser = arguments.get("use_browser", False)
        max_length = arguments.get("max_length", 8000)
        res = scrape_url(url=url, fields=fields, selector=selector, extract_mode=extract_mode, use_browser=use_browser, max_length=max_length)
        session_mgr.checkpoint()
        return res

    elif name == "scrape_moneycontrol":
        from tools.scraper_tools import scrape_moneycontrol
        query_or_ticker = state.company_name or state.ticker
        fields = arguments.get("fields")
        section = arguments.get("section", "overview")
        use_browser = arguments.get("use_browser", False)
        res = scrape_moneycontrol(query_or_ticker=query_or_ticker, fields=fields, section=section, use_browser=use_browser)
        if "overview_metrics" in res:
            state.market_data["moneycontrol_data"] = res["overview_metrics"]
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
            
        if state.score_results:
            if not any(s.key == "credit_scoring" for s in sec_specs):
                sec_specs.append(SectionSpec(
                    key="credit_scoring",
                    title="Credit Scoring & Governance Scorecard",
                    include=True,
                    order=len(sec_specs) + 1,
                    instruction="Auto-generated credit scoring template.",
                    emphasis=""
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
        from schemas import AnalystReviewStatus
        # Verify validation
        if not session_mgr.validation_result or not session_mgr.validation_result.satisfied:
            # Auto-validate once to check
            val_dict = await _dispatch_tool("validate_data", {})
            if not val_dict.get("satisfied"):
                return {
                    "error": "Cannot finalize report: required data validation unsatisfied.",
                    "validation": val_dict,
                }
                
        # Verify analyst review if scores were requested
        if state.score_results and state.analyst_review_status != AnalystReviewStatus.APPROVED:
            return {"error": "Cannot finalize report: Analyst Review is pending or rejected. You must call submit_for_analyst_review and wait for APPROVED status."}
        
        # Final validation logic
        from schemas import ScoreCategoryResult
        
        required_categories = {"Finances", "Business & Management", "Hygiene"}
        if state.sector_metrics and state.sector_metrics.sector.strip().lower() == "banking":
            required_categories.add("Banking")
            
        completed_categories = {res.score_category.value for res in state.score_results}
        
        if not state.credit_scoring_attempted:
            return {"error": "FATAL: Credit scoring was not attempted. You must run fetch_annual_report first to check for an annual report."}

        if not required_categories.issubset(completed_categories):
            if len(state.score_results) < 3:
                return {"error": "FATAL: You MUST score at least Finances, Business & Management, and Hygiene using get_category_text's fallback data, even if the PDF is missing!"}
                
            missing = required_categories - completed_categories
            state.custom_metrics["credit_scoring_unavailable"] = True
            logger.warning(f"Credit scoring incomplete. Missing categories: {missing}. Proceeding with finalized report because minimum 3 fallback categories are met.")

        if state.ticker and (state.ticker.endswith(".NS") or state.ticker.endswith(".BO")):
            required_tools = {"scrape_moneycontrol", "get_ownership", "compare_source_data"}
            successful_tools = {t.tool_name for t in state.tool_log if t.ok}
            missing = required_tools - successful_tools
            if missing:
                return {"error": f"Cannot finalize report: Indian equities require successful execution of {', '.join(missing)}."}

            # Enforce payload contents of compare_source_data
            comparison_calls = [call for call in state.tool_log if call.tool_name == "compare_source_data" and call.ok]
            if not comparison_calls:
                return {"error": "Missing compare_source_data execution. This is mandatory for Indian equities."}

            last_args = comparison_calls[-1].arguments
            mc_data = state.market_data.get("moneycontrol_data", {})
            yf_data = last_args.get("yfinance_data", {})

            # Force the agent to actually pass populated data
            if not mc_data or not yf_data:
                return {
                    "error": "compare_source_data was executed with empty dictionaries. You must successfully extract data via scrape_moneycontrol and get_price_snapshot/get_fundamentals first."
                }

            # --- Cross-Source State Reconciliation (Factual TTM Revenue Fix) ---
            yf_ttm = yf_data.get("revenue_ttm", 0) or 0
            mc_ttm = mc_data.get("revenue_ttm", 0) or 0
            
            if yf_ttm and mc_ttm:
                variance = abs(yf_ttm - mc_ttm) / max(yf_ttm, mc_ttm)
                if variance > 0.10:
                    if state.market_data:
                        state.market_data["revenue_ttm"] = mc_ttm
                        state.market_data["revenue_ttm_formatted"] = f"Rs. {mc_ttm/10000000:,.2f} Cr (Reconciled from Moneycontrol)"
                        logger.info(f"Reconciliation: Overwrote yfinance revenue_ttm with Moneycontrol figure (variance {variance:.2%})")

        state.custom_metrics["credit_scoring_source"] = "annual_report" if session_mgr.parsed_pages else "fallback_market_data"
        state.status = AgentStatus.DONE
        final_path = session_mgr.dump_final_session()
        return {
            "status": "finalized",
            "session_id": session_mgr.session_id,
            "final_payload_path": str(final_path),
            "ticker": state.ticker,
            "company_name": state.company_name,
        }

    elif name == "fetch_annual_report":
        from tools.annual_report_tools import fetch_annual_report
        res = fetch_annual_report(arguments["company_or_ticker"])
        state.credit_scoring_attempted = True
        session_mgr.checkpoint()
        return res
    elif name == "parse_report_text":
        from tools.annual_report_tools import parse_report_text
        pages = parse_report_text(arguments["pdf_path"])
        session_mgr.parsed_pages = pages
        return {"status": "success", "page_count": len(pages)}
    elif name == "run_ocr_fallback":
        from tools.annual_report_tools import run_ocr_fallback
        results = run_ocr_fallback(arguments["pdf_path"], arguments["page_numbers"])
        for r in results:
            for p in session_mgr.parsed_pages:
                if p["page_num"] == r["page_num"]:
                    p["text"] = r["text"]
        return {"status": "success", "ocr_pages": len(results)}
    elif name == "build_section_index":
        if not session_mgr.parsed_pages:
            from tools.annual_report_tools import build_fallback_dossier
            session_mgr.fallback_dossier = build_fallback_dossier(state)
            return {"status": "success", "message": "Fallback dossier generated"}
        else:
            from tools.annual_report_tools import build_section_index
            index = build_section_index(session_mgr.parsed_pages)
            session_mgr.bounded_index = index
            return index
    elif name == "get_promoter_holding":
        from tools.moneycontrol_tools import get_promoter_holding
        query_or_ticker = state.company_name or state.ticker or arguments.get("query_or_ticker")
        return get_promoter_holding(query_or_ticker)
    elif name == "get_shareholding_pattern":
        from tools.moneycontrol_tools import get_shareholding_pattern
        query_or_ticker = state.company_name or state.ticker or arguments.get("query_or_ticker")
        return get_shareholding_pattern(query_or_ticker)
    elif name == "get_board_composition":
        from tools.moneycontrol_tools import get_board_composition
        query_or_ticker = state.company_name or state.ticker or arguments.get("query_or_ticker")
        return get_board_composition(query_or_ticker)
    elif name == "submit_for_analyst_review":
        from schemas import AnalystReviewStatus
        pending_file = session_mgr.session_dir / "analyst_review_pending.json"
        response_file = session_mgr.session_dir / "analyst_review_response.json"
        
        pending_file.write_text(json.dumps(arguments), encoding="utf-8")
        state.analyst_review_status = AnalystReviewStatus.PENDING
        session_mgr.checkpoint()
        
        logger.info("submit_for_analyst_review invoked. Pausing MCP thread for human input...")
        wait_seconds = 0
        max_wait = 120  # 2 minute timeout

        while wait_seconds < max_wait:
            if response_file.exists():
                try:
                    resp = json.loads(response_file.read_text(encoding="utf-8"))
                    status = str(resp.get("status", "")).upper()
                    if status == "APPROVED":
                        state.analyst_review_status = AnalystReviewStatus.APPROVED
                        return {"status": "success", "instruction": "Approved. Proceed to finalize_report."}
                    elif status == "REJECTED":
                        state.analyst_review_status = AnalystReviewStatus.REJECTED
                        response_file.unlink(missing_ok=True)
                        pending_file.unlink(missing_ok=True)
                        return {"error": f"Analyst REJECTED the draft. Feedback: {resp.get('feedback')}. Fix this before resubmitting."}
                except json.JSONDecodeError:
                    pass
            await asyncio.sleep(0.5)  # Use asyncio.sleep instead of time.sleep
            wait_seconds += 0.5

        # If the loop exits without a response:
        return {"error": "TIMEOUT: Analyst did not respond. You MUST call submit_for_analyst_review again."}
    elif name == "get_category_text":
        category = arguments["category"]
        # Gate: reject invented category names BEFORE spending any LLM calls
        if category not in _VALID_CATEGORIES:
            return {
                "error": f"INVALID CATEGORY '{category}'. You MUST use exactly one of: "
                         f"{sorted(_VALID_CATEGORIES)}. Do not invent, paraphrase, or rename categories."
            }
        completed_categories = {r.score_category.value for r in state.score_results}
        if category in completed_categories:
            return {
                "status": "already_completed",
                "message": f"{category} was already scored and submitted in this session. "
                           f"Do NOT spawn another subagent for it — move on to the next category, "
                           f"or if all applicable categories are done, call submit_for_analyst_review.",
            }
        if session_mgr.active_subagent_category is not None:
            return {"error": f"ERROR: Subagent for {session_mgr.active_subagent_category} is already active. You MUST use submit_category_result to unlock it before spawning the next."}
        session_mgr.active_subagent_category = category

        if session_mgr.parsed_pages:
            p_nums = session_mgr.bounded_index.get(category, [])
            cat_text = ""
            for p_num in p_nums:
                page_text = next((p["text"] for p in session_mgr.parsed_pages if p["page_num"] == p_num), "")
                cat_text += f"\n--- PAGE {p_num} ---\n{page_text}"
            source = "annual_report"
        else:
            cat_text = session_mgr.fallback_dossier.get(category, "")
            source = "fallback_market_data"

        if not cat_text.strip():
            cat_text = (
                f"No annual-report or fallback data was available for {category}. "
                f"Score conservatively (score_value near 0.5) and state the data gap "
                f"explicitly in raw_evidence_snippets."
            )
            
        if os.environ.get("DSH_SUBAGENT_CONTEXT_DEBUG") == "1":
            try:
                debug_file = session_mgr.session_dir / f"subagent_context_{category.replace(' ', '_')}.json"
                debug_file.write_text(json.dumps({"text": cat_text, "source": source}, indent=2), encoding="utf-8")
            except Exception as e:
                logger.warning("Failed to write subagent context debug file: %s", e)
                
        return {"text": cat_text, "source": source}
    elif name == "submit_category_result":
        category = arguments["category"]
        # Gate: reject invented category names
        if category not in _VALID_CATEGORIES:
            return {
                "error": f"INVALID CATEGORY '{category}'. You MUST use exactly one of: "
                         f"{sorted(_VALID_CATEGORIES)}. Do not invent, paraphrase, or rename categories."
            }
        if session_mgr.active_subagent_category != category:
            return {"error": f"ERROR: Active subagent is {session_mgr.active_subagent_category}, but you tried to submit {category}."}
        from schemas import ScoreCategoryResult
        try:
            result = ScoreCategoryResult.model_validate(arguments["result"])
            async with _state_lock:
                state.score_results.append(result)
        except Exception as e:
            # Explicit lock check requirement
            return {"error": f"Invalid ScoreCategoryResult format: {e}. Note: Lock for {category} is still retained until a valid result is submitted."}
        session_mgr.active_subagent_category = None
        session_mgr.checkpoint()
        return {"status": "success", "message": f"{category} result stored. You may now terminate this subagent and spawn the next."}
    elif name == "compare_source_data":
        from tools.finance_tools import cross_check_source_agreement
        return cross_check_source_agreement(arguments["yfinance_data"], arguments["moneycontrol_data"])
    else:
        raise ValueError(f"Unknown MCP tool: {name}")


# ---------------------------------------------------------------------------
# MCP Handlers
# ---------------------------------------------------------------------------
NEW_TOOLS = [
    {
        "name": "fetch_annual_report",
        "description": "Downloads the annual report PDF for a company.",
        "inputSchema": {
            "type": "object",
            "properties": {"company_or_ticker": {"type": "string"}},
            "required": ["company_or_ticker"]
        }
    },
    {
        "name": "parse_report_text",
        "description": "Parses a PDF annual report page-by-page. Returns list of pages.",
        "inputSchema": {
            "type": "object",
            "properties": {"pdf_path": {"type": "string"}},
            "required": ["pdf_path"]
        }
    },
    {
        "name": "run_ocr_fallback",
        "description": "Targeted OCR fallback for empty or scanned pages.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pdf_path": {"type": "string"},
                "page_numbers": {"type": "array", "items": {"type": "integer"}}
            },
            "required": ["pdf_path", "page_numbers"]
        }
    },
    {
        "name": "build_section_index",
        "description": "Heuristic keyword matcher that returns strictly bounded page ranges for each ScoreCategory.",
        "inputSchema": {
            "type": "object",
            "properties": {"pages": {"type": "array", "items": {"type": "object"}}},
            "required": ["pages"]
        }
    },
    {
        "name": "get_promoter_holding",
        "description": "Fetches promoter holding data.",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "get_shareholding_pattern",
        "description": "Fetches broader shareholding pattern.",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "get_board_composition",
        "description": "Fetches board of directors / management team composition.",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "submit_for_analyst_review",
        "description": "Submits the draft for analyst review. Pauses execution.",
        "inputSchema": {
            "type": "object",
            "properties": {"draft_summary": {"type": "string"}},
            "required": ["draft_summary"]
        }
    },
    {
        "name": "get_category_text",
        "description": "Acquires the lock for a credit-scoring category and returns its bounded text. Enforces sequential execution. IMPORTANT: category MUST be one of the exact strings: 'Finances', 'Business & Management', 'Hygiene', 'Banking'. Any other value will be rejected.",
        "inputSchema": {
            "type": "object",
            "properties": {"category": {"type": "string", "enum": ["Finances", "Business & Management", "Hygiene", "Banking"]}},
            "required": ["category"]
        }
    },
    {
        "name": "submit_category_result",
        "description": "Releases the lock for a credit-scoring category and stores its score result. IMPORTANT: category MUST be one of the exact strings: 'Finances', 'Business & Management', 'Hygiene', 'Banking'.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": ["Finances", "Business & Management", "Hygiene", "Banking"]},
                "result": {"type": "object"}
            },
            "required": ["category", "result"]
        }
    },
    {
        "name": "compare_source_data",
        "description": "Cross-checks globally standard yfinance data against local Moneycontrol data to detect mismatches. Requires merged outputs from scrape_moneycontrol and get_promoter_holding.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "yfinance_data": {"type": "object"},
                "moneycontrol_data": {"type": "object"}
            },
            "required": ["yfinance_data", "moneycontrol_data"]
        }
    }
]

_existing_names = {t["name"] for t in TOOL_DEFINITIONS}
for t in NEW_TOOLS:
    if t["name"] not in _existing_names:
        TOOL_DEFINITIONS.append(t)
        _existing_names.add(t["name"])


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


def _match_candidate(selected: str, candidates: list[dict]) -> dict | None:
    if not candidates:
        return None
    sel_strip = selected.strip()
    if sel_strip.isdigit():
        idx = int(sel_strip) - 1
        if 0 <= idx < len(candidates):
            return candidates[idx]
            
    ticker_match = re.search(r"\(([A-Za-z0-9\.\^=-]+)\)", selected)
    extracted_sym = ticker_match.group(1).strip() if ticker_match else None
    
    if extracted_sym:
        for c in candidates:
            if c["ticker"].lower() == extracted_sym.lower():
                return c
                
    for c in candidates:
        if c["ticker"].lower() in selected.lower() or c["name"].lower() in selected.lower():
            return c
            
    return None


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
        result = await _dispatch_tool(name, arguments)
        rec.result_summary = str(result)[:200]
        if isinstance(result, dict) and "error" in result:
            rec.ok = False
            rec.error = str(result["error"])[:500]
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
