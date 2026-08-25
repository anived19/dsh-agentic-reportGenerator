"""
MCP (Model Context Protocol) tool server for the Financial Report Generator.

Exposes all deterministic tools (finance, AML, search, entity resolution)
as MCP tools over stdio transport. The DSH runtime connects to this server
via Cordis configuration to make these tools available to the agent.

Run standalone for testing:
    python -m harness.mcp_server
"""
from __future__ import annotations

import json
import logging
import sys
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logger = logging.getLogger(__name__)

# ---- MCP Server Instance ----
server = Server("finoscale-report-tools")


# ---------------------------------------------------------------------------
# Tool definitions — JSON Schema descriptions for the DSH runtime
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "resolve_entity",
        "description": (
            "Resolve a natural-language company or conglomerate reference to "
            "validated candidate ticker symbols. Returns a list of candidate "
            "dicts with ticker, name, exchange, sector, confidence."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Company name, ticker, or group reference (e.g. 'Tata', 'TCS', 'Reliance Industries')."
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_price_snapshot",
        "description": (
            "Fetch current price, market cap, 50d/200d moving averages, "
            "and outlook high/low for a given ticker. Returns structured "
            "numeric data from yfinance — never hallucinated."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {
                    "type": "string",
                    "description": "Stock ticker symbol (e.g. 'TCS.NS', 'AAPL', 'RELIANCE.NS')."
                }
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_valuation_multiples",
        "description": (
            "Fetch valuation multiples: P/E, forward P/E, P/B, P/S, "
            "EV/EBITDA, dividend yield, revenue TTM, and margins."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {
                    "type": "string",
                    "description": "Stock ticker symbol."
                }
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_fundamentals",
        "description": (
            "Fetch EPS (TTM), debt-to-equity, ROE, ROCE, and broker "
            "analyst consensus ratings (buy/hold/sell counts, price targets)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {
                    "type": "string",
                    "description": "Stock ticker symbol."
                }
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_quarterly_financials",
        "description": (
            "Fetch quarterly financials (revenue, net income, QoQ growth) "
            "for the last 4 quarters."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {
                    "type": "string",
                    "description": "Stock ticker symbol."
                }
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_technicals",
        "description": (
            "Fetch technical analysis metrics: RSI-14, MACD (line/signal/histogram), "
            "volume trend, and support/resistance levels."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {
                    "type": "string",
                    "description": "Stock ticker symbol."
                }
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_ownership",
        "description": (
            "Fetch promoter/insider, institutional, and public holding "
            "percentages for a given ticker."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {
                    "type": "string",
                    "description": "Stock ticker symbol."
                }
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "search_web_news",
        "description": (
            "Search the live web for financial news and sentiment relevant "
            "to a query. Uses Tavily search API. Each call counts against "
            "the shared Tavily budget."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query for financial news/sentiment."
                },
                "ticker": {
                    "type": "string",
                    "description": "Optional ticker to prepend to the query if not already included.",
                    "default": ""
                },
                "depth": {
                    "type": "string",
                    "description": "Search depth: 'basic' (1 credit) or 'advanced' (2 credits).",
                    "enum": ["basic", "advanced"],
                    "default": "basic"
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "run_structured_aml_sweep",
        "description": (
            "Bundled deterministic AML/ABC compliance sweep. Screens the entity "
            "against OFAC SDN, OpenSanctions, World Bank debarment, UN Consolidated "
            "List, EU Sanctions, SEC EDGAR FCPA releases, TI CPI jurisdictional risk, "
            "and FATF grey/black list — all in parallel in one call. All sources are "
            "free and publicly accessible."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity_name": {
                    "type": "string",
                    "description": "Company or person name to screen."
                },
                "ticker": {
                    "type": "string",
                    "description": "Optional ticker for jurisdictional risk determination.",
                    "default": ""
                },
            },
            "required": ["entity_name"],
        },
    },
    {
        "name": "search_adverse_media",
        "description": (
            "Tavily-backed adverse media search with secondary LLM verification. "
            "Searches for regulatory enforcement actions, SEBI orders, ED raids, "
            "bribery/corruption investigations. Results are LLM-filtered to remove "
            "noise. Each call counts against the shared Tavily budget."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity_name": {
                    "type": "string",
                    "description": "Company or person name to search for adverse media."
                },
                "focus": {
                    "type": "string",
                    "description": "Optional specific focus area for the search (e.g. 'SEBI order').",
                    "default": ""
                },
                "depth": {
                    "type": "string",
                    "description": "Search depth: 'basic' or 'advanced'.",
                    "enum": ["basic", "advanced"],
                    "default": "basic"
                },
            },
            "required": ["entity_name"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool dispatch — routes MCP tool calls to existing Python functions
# ---------------------------------------------------------------------------

def _dispatch_tool(name: str, arguments: dict[str, Any]) -> Any:
    """Route an MCP tool call to the appropriate Python function."""
    # Lazy imports to avoid circular dependencies and heavy startup costs
    if name == "resolve_entity":
        from tools.ticker_resolver import resolve_entity
        return resolve_entity(arguments["query"])

    elif name == "get_price_snapshot":
        from tools.finance_tools import get_price_snapshot
        return get_price_snapshot(arguments["ticker"])

    elif name == "get_valuation_multiples":
        from tools.finance_tools import get_valuation_multiples
        return get_valuation_multiples(arguments["ticker"])

    elif name == "get_fundamentals":
        from tools.finance_tools import get_fundamentals
        return get_fundamentals(arguments["ticker"])

    elif name == "get_quarterly_financials":
        from tools.finance_tools import get_quarterly_financials
        return get_quarterly_financials(arguments["ticker"])

    elif name == "get_technicals":
        from tools.finance_tools import get_technicals
        return get_technicals(arguments["ticker"])

    elif name == "get_ownership":
        from tools.finance_tools import get_ownership
        return get_ownership(arguments["ticker"])

    elif name == "search_web_news":
        from tools.search_tools import search_web_news
        return search_web_news(
            query=arguments["query"],
            ticker=arguments.get("ticker", ""),
            depth=arguments.get("depth", "basic"),
        )

    elif name == "run_structured_aml_sweep":
        from tools.aml_tools import run_structured_aml_sweep
        return run_structured_aml_sweep(
            entity_name=arguments["entity_name"],
            ticker=arguments.get("ticker", ""),
        )

    elif name == "search_adverse_media":
        from tools.aml_tools import search_adverse_media
        return search_adverse_media(
            entity_name=arguments["entity_name"],
            focus=arguments.get("focus", ""),
            depth=arguments.get("depth", "basic"),
        )

    else:
        raise ValueError(f"Unknown tool: {name}")


# ---------------------------------------------------------------------------
# MCP server handlers
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[Tool]:
    """Return all available tools."""
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
    """Execute a tool and return the result as JSON text."""
    arguments = arguments or {}
    logger.info("MCP tool call: %s(%s)", name, arguments)

    try:
        result = _dispatch_tool(name, arguments)
        result_json = json.dumps(result, default=str, ensure_ascii=False)
        return [TextContent(type="text", text=result_json)]
    except Exception as exc:
        logger.exception("MCP tool %s failed", name)
        error_json = json.dumps({"error": str(exc)}, ensure_ascii=False)
        return [TextContent(type="text", text=error_json)]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    """Run the MCP server over stdio."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,   # MCP uses stdout for JSON-RPC; logs go to stderr
    )
    logger.info("Starting Finoscale MCP tool server (stdio transport)...")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
