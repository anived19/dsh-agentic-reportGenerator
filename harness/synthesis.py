"""
Chief Editor: single-shot synthesis call. No tools, no autonomy, no
multi-turn loop — deliberately not agentic.

There's no decision left to make at this stage: Market Data and Sentiment
Findings have already been fetched and validated. Giving this stage tool
access or iterative autonomy would only add a surface for it to restate a
number incorrectly, with no corresponding benefit. It reads the two
validated JSON objects and produces Markdown — nothing more.

The report_type parameter controls which sections the Chief Editor is
instructed to include. Section ordering is now driven by render_config.yaml
rather than hardcoded strings — adding a new section or changing order
requires only an edit to that config file, not a code change here.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml
from google import genai
from google.genai import types

from config import settings
from harness.gemini_retry import generate_with_retry
from harness.md_loader import load_agent_prompt
from schemas import (
    AMLFinding,
    AMLScreeningResult,
    AMLSeverity,
    MarketMetrics,
    ReportSpec,
    ReportType,
    SentimentFindings,
)

logger = logging.getLogger(__name__)

# Load render configuration once at module import time.
# Falls back to a minimal inline config if the file is missing, so an
# absent render_config.yaml degrades gracefully rather than crashing.
_RENDER_CONFIG_PATH = Path("render_config.yaml")

def _load_render_config() -> dict:
    if _RENDER_CONFIG_PATH.exists():
        try:
            with open(_RENDER_CONFIG_PATH, encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception as exc:
            logger.warning("Could not load render_config.yaml: %s — using inline defaults", exc)
    return {}

_RENDER_CONFIG: dict = _load_render_config()


# ---------------------------------------------------------------------------
# Section heading map (maps config key -> Markdown heading text)
# Keep in sync with render_config.yaml section_specs.
# ---------------------------------------------------------------------------
_SECTION_HEADINGS: dict[str, str] = {
    "executive_summary":       "# Executive Summary",
    "financial_highlights":    "## Financial Highlights",
    "fundamentals_deep_dive":  "## Fundamentals Deep-Dive",
    "technicals":              "## Technical Analysis",
    "holdings":                "## Ownership & Holdings",
    "valuation_analysis":      "## Valuation Analysis",
    "sentiment_news":          "## Market Sentiment & News",
    "risk_factors":            "## Risk Factors",
    "scenario_outlook":        "## {n}-Month Outlook",   # {n} filled at runtime
    "peer_benchmarking":       "## Peer Benchmarking & Industry Analysis",
    "sector_kpis":             "## Specialized Sector Performance KPIs",
    "credit_scoring":          "## Credit Scoring & Governance Scorecard",
}


# ---------------------------------------------------------------------------
# Section instruction builders — one function per section type.
# Each returns a plain-English instruction string fed to the Chief Editor.
# ---------------------------------------------------------------------------

def _instr_credit_scoring(**kwargs) -> str:
    return (
        "Write a Credit Scoring & Governance Scorecard section summarizing the 4-pillar subagent evaluation. "
        "Format as a Markdown table with columns: Category | Score | Summary Verdict. "
        "The categories are Finances, Business & Management, Hygiene, and Banking. "
        "Use the exact score_value and summary from the score_results array. "
        "Below the table, provide a brief 2-3 sentence overall governance and risk verdict based on the scores."
    )

def _instr_executive_summary(outlook_label: str, market_metrics: Optional[MarketMetrics] = None, **kwargs) -> str:
    breakout_text = f"breakout status '{market_metrics.breakout_status}'" if market_metrics and market_metrics.breakout_status else "range-bound/breakout positioning"
    return (
        "Write a concise Executive Summary (3–5 sentences) covering: the company's "
        "current market position, synthesized fundamental vs technical stance, primary driver of near-term outlook, "
        f"and technical positioning (incorporating {breakout_text}).\n"
        "CRITICAL RECONCILIATION RULE: Cross-verify news sentiment against technical trend flags. "
        "If news sentiment is Bullish but the price is trading significantly below its 200-day MA or RSI/MACD shows selling pressure, "
        "synthesize this accurately (e.g. 'Attractive fundamental valuation amidst medium-term technical consolidation/pullback') "
        "rather than asserting an unhedged 'Market sentiment remains bullish'. Never state conflicting narrative and technical claims!\n"
        "CRITICAL RULE: NEVER claim a 'Technical Breakdown' or 'Technical Breakout' if the price is between support and resistance! "
        "Do not repeat numbers that appear in other sections — the summary should read as a standalone verdict, not a data recitation. "
        "Any currency or large numeric figure mentioned in this section (market cap, revenue, price) "
        "must be copied character-for-character from its corresponding *_formatted field in the JSON "
        "(e.g. market_cap_formatted) — never re-derive or re-type the number from the raw numeric field."
    )

def _instr_financial_highlights(outlook_label: str, **kwargs) -> str:
    return (
        f"Write a Financial Highlights table: Metric | Value | Notes. "
        f"Rows: Current Price, Market Cap, 50-Day MA, 200-Day MA, "
        f"{outlook_label} High, {outlook_label} Low. "
        f"Always use the pre-formatted fields from the JSON character-for-character "
        f"(e.g. current_price_formatted, market_cap_formatted, fifty_day_ma_formatted, "
        f"two_hundred_day_ma_formatted, outlook_high_formatted, outlook_low_formatted) — "
        f"never display raw scientific notation or unrounded decimals. "
        f"ANTI-WRAPPING RULE: Notes column must ONLY contain short 2-4 word factual labels (e.g. 'above 50d MA', 'below 200d MA'). "
        f"NEVER put sentences, commentary, or URLs inside table cells. "
        f"Source note on table caption: '(Source: Yahoo Finance via yfinance)'"
    )

def _instr_fundamentals_deep_dive(outlook_label: str, market_metrics: Optional[MarketMetrics] = None, **kwargs) -> str:
    is_bank = market_metrics and market_metrics.is_bank_equity
    dte_guidance = (
        "   For Debt-to-Equity: Depository and commercial banks are governed by Tier 1 capital regulations and do not report corporate Debt/Equity; use debt_to_equity_formatted."
        if is_bank else
        "   For Debt-to-Equity: use debt_to_equity_formatted (e.g. '0.10x (10.21%)'). Note in the narrative that corporate leverage is expressed as a ratio (e.g. 0.10x indicates negligible debt burden, where 10.21% is the raw debt-to-equity percentage). Never describe a company with 0.10x D/E as heavily leveraged!"
    )
    reconcil_guidance = (
        f"   TTM RECONCILIATION NOTE: {market_metrics.ttm_reconciliation_note}\n"
        if market_metrics and market_metrics.ttm_reconciliation_note else ""
    )

    return (
        "Write a Fundamentals Deep-Dive section with three sub-tables:\n"
        "1. Key Metrics table (Metric | Value | Notes): EPS (TTM), Dividend Yield, "
        "   Debt-to-Equity, Return on Equity (ROE), Return on Capital Employed (ROCE). "
        "   CRITICAL UNIT FORMATTING: Always use formatted percentage strings for ROE, ROCE, and Dividend Yield "
        "   (e.g. roe_formatted '47.74%', roce_formatted '54.93%', dividend_yield_formatted '2.75%'). "
        "   Use eps_ttm_formatted and debt_to_equity_formatted.\n"
        f"{dte_guidance}\n"
        "   Notes column: concise 2–4 word labels only. For any field in unavailable_fields, write 'data unavailable'.\n"
        "2. Analyst Consensus table (Metric | Value): Buy count, Hold count, Sell count, "
        "   Mean price target, High price target, Low price target, Recommendation. "
        "   Any analyst price target figure (mean, high, low) must be copied character-for-character "
        "   from its corresponding *_formatted field in the JSON (e.g. analyst_target_mean_formatted, "
        "   analyst_target_high_formatted, analyst_target_low_formatted such as '2,456.12') — never re-derive "
        "   or output raw unrounded decimal figures like '2456.122'. "
        "   If analyst fields are unavailable, say so. Cite Yahoo Finance as the source.\n"
        "3. Quarterly Financials table (Quarter | Revenue | Net Income | Rev QoQ % | Profit QoQ %): "
        "   use the quarterly_financials array (newest first). Ensure a 4-quarter rolling baseline is presented.\n"
        f"{reconcil_guidance}"
        "   CRITICAL FORMATTING: For Revenue and Net Income, use revenue_formatted and net_income_formatted "
        "   (e.g. 'Rs. 72,275 Cr', '$52.85B' — NEVER display unscaled 12-digit numbers). "
        "   For Rev QoQ % and Profit QoQ %, use revenue_growth_qoq_formatted and profit_growth_qoq_formatted "
        "   (e.g. '+2.23%', '-2.69%', or 'data unavailable'). Never omit the '+' or '-' sign or '%' symbol. "
        "   If Net Income exceeds Revenue in a quarter (due to exceptional one-off gains, demergers, or discontinued operations), "
        "   add an explanatory asterisk note below the table: '*(Note: Net income for [Quarter] includes extraordinary/one-off items or demerger accounting gains)*'. "
        "   If any QuarterlyDataPoint has a data_gap_note, surface it as a visible footnote under the table (e.g. '*Note: A prior quarter may be missing from source data (yfinance).*'). "
        "   This is the only place the Quarterly Financials table appears in the report — do not repeat it in the Executive Summary or Outlook sections.\n"
        "   Source note: '(Source: Yahoo Finance via yfinance)'"
    )

def _instr_technicals(outlook_label: str, market_metrics: Optional[MarketMetrics] = None, **kwargs) -> str:
    breakout_val = market_metrics.breakout_status if market_metrics and market_metrics.breakout_status else None
    breakout_note = f"\n- Calculated Breakout Assessment: '{breakout_val}' (Price vs Support/Resistance with 0.5% clearance filter and volume expansion check)." if breakout_val else ""
    return (
        "Write a Technical Analysis section covering:\n"
        "- RSI-14: state the exact value, then interpret (>70 overbought, <30 oversold, 30–70 neutral). "
        "  These levels are statistical reference points, not trading signals.\n"
        "- MACD (12/26/9): state line, signal, and histogram values. "
        "  Interpret as bullish/bearish momentum only if the numbers clearly support it.\n"
        "- Volume & Delivery: state the 20-day average volume, volume trend (rising/falling/flat vs. 20-day average), "
        "  and the 20-Day Average Delivery % (using twenty_day_avg_delivery_pct_formatted). Interpret what the delivery percentage implies about accumulation/distribution.\n"
        "- VWAP: state the Volume Weighted Average Price (using vwap_formatted) and note if the current price is trading above or below it.\n"
        f"- Support & Resistance: state the derived levels (10th/90th percentile of the "
        f"  {outlook_label.lower()} price range). Note: 'These are statistically derived "
        f"  reference levels, not broker recommendations.'\n"
        "- Breakout/Breakdown Threshold Check: "
        "  * Confirmed Technical Breakout requires Current Price >= Resistance * 1.005 (0.5% clearance) AND volume confirmation. "
        "  * If price is above resistance but within 0.5%, classify as 'Testing Resistance Boundary'. "
        "  * Confirmed Technical Breakdown requires Current Price <= Support * 0.995 AND volume expansion. "
        "  * If Support <= Current Price <= Resistance: state price is trading within its normal statistical channel. "
        "    NEVER claim a breakout or breakdown when price is between support and resistance!"
        f"{breakout_note}\n"
        "For any unavailable technical field, state it explicitly — do not omit or estimate."
    )

def _instr_holdings(outlook_label: str, market_metrics: Optional[MarketMetrics] = None, **kwargs) -> str:
    is_us = market_metrics and market_metrics.is_us_equity
    insider_label = market_metrics.insider_holding_label if market_metrics and market_metrics.insider_holding_label else ("Insider Ownership (SEC Form 4/10-K)" if is_us else "Promoter Holding")
    inst_label = market_metrics.institutional_holding_label if market_metrics and market_metrics.institutional_holding_label else ("Institutional Ownership (SEC Form 13F)" if is_us else "Institutional Holding (FII + DII)")
    filing_note = market_metrics.jurisdiction_filing_note if market_metrics and market_metrics.jurisdiction_filing_note else (
        "Institutional holdings aggregated from SEC Form 13F filings via Yahoo Finance. Insider holdings reflect Form 4/144 beneficial ownership."
        if is_us else
        "Institutional figure is the combined FII+DII total as reported by Yahoo Finance. Individual FII and DII breakdown requires BSE/NSE exchange filings."
    )

    return (
        "Write an Ownership & Holdings section:\n"
        f"- Table: Holder Category | % Held. Rows: {insider_label}, {inst_label}, Public Float.\n"
        "- Use promoter_holding_pct_formatted, institutional_holding_pct_formatted, and public_holding_pct_formatted "
        "  (e.g. '0.39%', '75.83%', '23.78%' or '71.80%', '17.45%', '10.75%'). The sum of the percentages MUST mathematically equal 100.00%.\n"
        "- JURISDICTION & REGULATORY ACCURACY: For US equities (e.g. JPM, AAPL), NEVER use Indian SEBI terminology like 'Promoter Holding' or 'BSE/NSE exchange filings'. "
        "  Use SEC terminology (Form 13F institutional holdings, Form 4 insider ownership).\n"
        f"- Footnote / Disclosures: '{filing_note}'\n"
        "Source: '(Source: Yahoo Finance via yfinance)'"
    )

def _instr_valuation_analysis(outlook_label: str, market_metrics: Optional[MarketMetrics] = None, **kwargs) -> str:
    is_bank = market_metrics and market_metrics.is_bank_equity
    bank_note = (
        "\nBANKING METRIC RULE: For depository / commercial banks, Gross Margin is 'N/A (Depository Bank - No COGS)' "
        "and EV/EBITDA is 'N/A (Depository Bank - Operating Interest)'. Traditional depository institutions do not incur standard Cost of Goods Sold (COGS), "
        "and interest expense is an operational line item rather than a financing cost, rendering Enterprise Value and EBITDA structurally inapplicable. "
        "Explain this in the table notes or narrative.\n"
        if is_bank else ""
    )

    peer_instr = ""
    if market_metrics and market_metrics.peer_benchmarks and market_metrics.peer_benchmarks.peers:
        peer_data = market_metrics.peer_benchmarks
        peers_list = ", ".join([f"{p.name} ({p.ticker})" for p in peer_data.peers])
        peer_instr = (
            f"\n\nPEER BENCHMARKING TABLE (MANDATORY WHEN REQUESTED IN VALUATION/PEER SECTIONS):\n"
            f"- Table: Company | Market Cap | Trailing P/E | Forward P/E | P/S | EV/EBITDA | Operating Margin.\n"
            f"- Rows: Target Company ({market_metrics.company_name or market_metrics.ticker}) and Peer Competitors ({peers_list}).\n"
            f"- Copy exact formatted values from peer_benchmarks JSON (e.g. pe_ratio_formatted, forward_pe_formatted, ps_ratio_formatted, ev_ebitda_formatted, operating_margin_formatted).\n"
            f"- Synthesize relative valuation positioning: identify whether the target company trades at a premium or discount to industry peers.\n"
        )

    return (
        "Write a Valuation Analysis table: Metric | Value | Notes. "
        "Rows: P/E (Trailing), P/E (Forward), Price-to-Book, Price-to-Sales, "
        "EV/EBITDA, Dividend Yield, EPS (TTM), Revenue (TTM), Gross Margin, "
        "Operating Margin, Beta.\n"
        f"{bank_note}"
        "CRITICAL FORMATTING RULES:\n"
        "- Valuation multiples: copy pe_ratio_formatted, forward_pe_formatted, pb_ratio_formatted, "
        "  ps_ratio_formatted, ev_ebitda_formatted, beta_formatted — never output raw unrounded floats like '17.152199'.\n"
        "- Percentage ratios: copy gross_margin_formatted, operating_margin_formatted, and dividend_yield_formatted — "
        "  never output raw decimal fractions like '0.40389' or '0.23963'. Dividend yield e.g. '0.46%' or '2.82%' must be exact.\n"
        "- Revenue (TTM): copy revenue_ttm_formatted (e.g. 'Rs. 2.76 Lakh Cr' or '$194.91B').\n"
        "- ANTI-WRAPPING RULE (TABLE CELL ECONOMY): Notes column must ONLY contain short 2-4 word factual notes (e.g. 'below 5yr avg', 'in line with sector'). "
        "  NEVER place full sentences, analyst commentary, or URL citations ([Source: ...]) inside table cells! "
        "  All analyst quotes, valuation narratives, and URL citations MUST be placed in dedicated standard paragraphs or bullet points under Market Sentiment & News / Key Catalysts.\n"
        f"{peer_instr}"
        "Source note: '(Source: Yahoo Finance via yfinance)'"
    )

def _instr_sentiment_news(outlook_label: str, sentiment_findings: Optional[SentimentFindings] = None, **kwargs) -> str:
    if sentiment_findings and sentiment_findings.extraction_failed:
        return (
            "Write a Market Sentiment & News section stating plainly that automated sentiment extraction "
            "did not complete successfully for this run and that no cited catalysts or risks could be structured "
            "from search results. State clearly: 'Automated sentiment extraction did not complete successfully for "
            "this run; no catalysts or risks could be structured from search results.' Do not provide a "
            "Bullish/Bearish/Neutral market mood verdict or fabricate news catalysts."
        )
    return (
        "Write a Market Sentiment & News section:\n"
        "- Open with the overall_sentiment label and sentiment_summary (1–2 sentences).\n"
        "- Key Catalysts sub-heading: bulleted list of key_catalysts, each ending in "
        "  '[Source: URL]' using the exact source_url from the JSON. Do not alter URLs.\n"
        "- Key Risks sub-heading: bulleted list of short-term news-driven risk catalysts from recent search results, "
        "  each ending in '[Source: URL]'.\n"
        "- Ensure currency symbols match the target company's reporting currency (e.g. Rs./INR for Indian equities, $ for US equities).\n"
        "Do not introduce any claim not in the sentiment findings JSON."
    )

def _instr_risk_factors(outlook_label: str, **kwargs) -> str:
    return (
        "Write a Risk Factors section (covering structural, strategic, competitive, and macroeconomic risks):\n"
        "- Analyze 3–5 distinct structural business and industry risks (e.g. macroeconomic slowdown in key geographic markets, "
        "  client concentration in banking/financial services, margin pressure from wage inflation, technological transition/disruption).\n"
        "- RELATIVE IMPACT SIZING: For legal penalties, one-off charges, or regulatory fines, provide relative sizing against annual or quarterly net income (e.g. explicitly noting if a one-time penalty represents <2% of quarterly net profit) rather than treating raw headlines with equal unweighted alarm.\n"
        "- Do NOT copy-paste or duplicate the exact same short-term news bullets from Market Sentiment & News — "
        "  this section must provide broader, medium-to-long term fundamental risk analysis.\n"
        "- Close with: 'Risk assessment is based on public disclosures, market conditions, and analytical synthesis.'"
    )

def _instr_scenario_outlook(outlook_label: str, **kwargs) -> str:
    return (
        f"Write a {outlook_label} Outlook section using the Bull/Base/Bear structure:\n"
        "**Bull Case** (2–4 sentences): describe the upside scenario tied to a specific "
        "catalyst or metric already in this report (e.g. RSI level, MACD crossover, "
        "a cited positive catalyst). Use hedged language ('could', 'may', 'if X materializes').\n"
        "**Base Case** (2–4 sentences): the most likely path based on current data — "
        "balanced view of technicals + sentiment + valuation. Hedged language required.\n"
        "**Bear Case** (2–4 sentences): the downside scenario tied to a specific cited "
        "risk or technical weakness. Hedged language required.\n"
        "Close with one sentence: 'This outlook is an analytical synthesis of current "
        "publicly available data and does not constitute a prediction or investment advice.'"
    )

def _instr_peer_benchmarking(outlook_label: str, market_metrics: Optional[MarketMetrics] = None, **kwargs) -> str:
    peer_data = market_metrics.peer_benchmarks if market_metrics else None
    if not peer_data or not peer_data.peers:
        return (
            "Write a Peer Benchmarking & Industry Comparison section:\n"
            "- Compare the target company's valuation multiples and operating margins against sector norms.\n"
            "- Note: 'Peer comparison based on sector averages and market benchmarks.'"
        )
    peers_list = ", ".join([f"{p.name} ({p.ticker})" for p in peer_data.peers])
    return (
        "Write a Peer Benchmarking & Comparative Industry Analysis section:\n"
        f"- Table: Company | Market Cap | Trailing P/E | Forward P/E | P/S | EV/EBITDA | Operating Margin.\n"
        f"- Target company and peers: {peers_list}.\n"
        "- Use the exact formatted values from the JSON (market_cap_formatted, pe_ratio_formatted, forward_pe_formatted, ps_ratio_formatted, ev_ebitda_formatted, operating_margin_formatted).\n"
        "- Provide a concise comparative synthesis (2-3 paragraphs) highlighting relative valuation premiums or discounts, margin leadership, and market positioning.\n"
        "- Source note: '(Source: Yahoo Finance peer market data)'"
    )

def _instr_sector_kpis(outlook_label: str, market_metrics: Optional[MarketMetrics] = None, **kwargs) -> str:
    sec = market_metrics.sector_metrics if market_metrics else None
    if sec and sec.banking:
        b = sec.banking
        return (
            "Write a Specialized Banking & Financial Performance KPIs section:\n"
            "- Table: Banking Metric | Value | Analytical Context.\n"
            f"  Rows: Net Interest Margin (NIM) Proxy ({b.nim_formatted or 'N/A'}), Efficiency Ratio ({b.efficiency_ratio_formatted or 'N/A'}), "
            f"  Return on Assets ({b.roa_formatted or 'N/A'}), Equity-to-Assets Capital Ratio ({b.equity_to_assets_formatted or 'N/A'}).\n"
            "- Explain how the cost-to-income efficiency ratio and capital adequacy support balance sheet resilience.\n"
            "- Source: '(Source: Deterministic banking calculation from public financials)'"
        )
    elif sec and sec.saas:
        s = sec.saas
        return (
            "Write a Specialized SaaS & Cloud Growth Performance section:\n"
            "- Table: SaaS KPI | Value | Benchmark Context.\n"
            f"  Rows: Rule of 40 Score ({s.rule_of_40_formatted or 'N/A'} - {s.rule_of_40_status or 'N/A'}), ARR Run-Rate ({s.arr_run_rate_formatted or 'N/A'}), "
            f"  Free Cash Flow Margin ({s.fcf_margin_formatted or 'N/A'}), Revenue per Employee ({s.revenue_per_employee_formatted or 'N/A'}).\n"
            "- Synthesize whether software growth velocity combined with free cash flow margin exceeds the 40% institutional hurdle rate.\n"
            "- Source: '(Source: Deterministic SaaS calculation from quarterly financials)'"
        )
    elif sec and sec.retail:
        r = sec.retail
        return (
            "Write an Operational Velocity & Capital Efficiency section:\n"
            "- Table: Operational Metric | Value | Context.\n"
            f"  Rows: Asset Turnover ({r.asset_turnover_formatted or 'N/A'}), Gross Margin Stability ({r.gross_margin_stability or 'N/A'}).\n"
            "- Synthesize inventory velocity, asset utilization, and supply chain pricing stability.\n"
            "- Source: '(Source: Deterministic operational calculation from balance sheet)'"
        )
    else:
        return (
            "Write a Specialized Sector Performance section analyzing industry-specific operating efficiency and capital allocation."
        )


_SECTION_INSTRUCTION_MAP = {
    "executive_summary":       _instr_executive_summary,
    "financial_highlights":    _instr_financial_highlights,
    "fundamentals_deep_dive":  _instr_fundamentals_deep_dive,
    "technicals":              _instr_technicals,
    "holdings":                _instr_holdings,
    "valuation_analysis":      _instr_valuation_analysis,
    "sentiment_news":          _instr_sentiment_news,
    "risk_factors":            _instr_risk_factors,
    "scenario_outlook":        _instr_scenario_outlook,
    "peer_benchmarking":       _instr_peer_benchmarking,
    "sector_kpis":             _instr_sector_kpis,
    "credit_scoring":          _instr_credit_scoring,
}


def _build_section_instructions(
    report_type: ReportType,
    outlook_label: str,
    market_metrics: Optional[MarketMetrics] = None,
    report_spec: Optional[ReportSpec] = None,
    sentiment_findings: Optional[SentimentFindings] = None,
    editorial_goal: Optional[str] = None,
) -> str:
    """
    Build the ordered list of section instructions for the Chief Editor.
    If a ReportSpec is provided by the orchestrator, its sections and emphasis directives
    override the default render_config.yaml list.
    """
    goal_header = f"EDITORIAL GOAL / FRAMING: {editorial_goal}\n" if editorial_goal else ""

    if report_spec and report_spec.sections:
        active_sections = sorted(
            [s for s in report_spec.sections if s.include],
            key=lambda x: x.order,
        )
        lines: list[str] = [
            f"This is a {report_type.value.upper()} report with custom agentic formatting.",
            goal_header.strip(),
            "Include EXACTLY these sections in EXACTLY this order, adhering to word budget (120-180 words/section) and specific emphasis directives:",
        ]
        for spec in active_sections:
            if spec.title:
                heading = spec.title if spec.title.startswith("#") else f"## {spec.title}"
            else:
                heading = _SECTION_HEADINGS.get(spec.key, f"## {spec.key.replace('_', ' ').title()}")
            heading = heading.replace("{n}", str(outlook_label.split("-")[0]))

            if spec.instruction:
                base_instruction = spec.instruction
                # If custom instruction asks for peer benchmarking and peer data exists, append peer rows table
                if any(k in (spec.key.lower() + (spec.title or "").lower()) for k in ("peer", "benchmark")) and market_metrics and market_metrics.peer_benchmarks:
                    peer_sec = _instr_peer_benchmarking(outlook_label, market_metrics=market_metrics)
                    if "PEER BENCHMARKING" not in base_instruction.upper():
                        base_instruction = f"{base_instruction}\n\n{peer_sec}"
            else:
                if any(k in (spec.key.lower() + (spec.title or "").lower()) for k in ("peer", "benchmark")):
                    base_instruction = _instr_peer_benchmarking(outlook_label, market_metrics=market_metrics)
                else:
                    instruction_fn = _SECTION_INSTRUCTION_MAP.get(spec.key)
                    if instruction_fn:
                        base_instruction = instruction_fn(outlook_label, market_metrics=market_metrics, sentiment_findings=sentiment_findings)
                    else:
                        # Dynamic instruction for custom section
                        sec_title = spec.key.replace("_", " ").title()
                        base_instruction = (
                            f"Write a focused {sec_title} section addressing the report's editorial goal.\n"
                            f"- Use clean Markdown headings (H2/H3), concise standard paragraphs, or compact data tables.\n"
                            f"- ANTI-WRAPPING RULE: Any table cells in this custom section must ONLY contain concise values/notes (2-4 words). "
                            f"  Move all narrative, strategic commentary, and cited source URLs into standard narrative paragraphs or bullet points.\n"
                            f"- NUMERIC FIDELITY: Copy all numeric figures character-for-character from the JSON (including custom_metrics); never output unrounded raw floats or scientific notation.\n"
                            f"- WORD BUDGET: Maintain concise executive focus (120–180 words, max 6–8 table rows)."
                        )

            emphasis_directive = f"\n[EDITORIAL EMPHASIS DIRECTIVE]: {spec.emphasis}" if spec.emphasis else ""
            lines.append(f"\n{heading}\n{base_instruction}{emphasis_directive}")
        return "\n".join(lines)

    # Fallback to render_config.yaml
    config_sections: list[str] | None = None
    try:
        config_sections = (
            _RENDER_CONFIG
            .get("report_types", {})
            .get(report_type.value, {})
            .get("layer1_sections")
        )
    except Exception:
        pass

    # Inline defaults (mirrors render_config.yaml) — used if config file is absent
    _defaults: dict[ReportType, list[str]] = {
        ReportType.SENTIMENT: [
            "executive_summary", "sentiment_news", "risk_factors", "scenario_outlook",
        ],
        ReportType.VALUATION: [
            "executive_summary", "financial_highlights", "fundamentals_deep_dive",
            "holdings", "valuation_analysis", "scenario_outlook",
        ],
        ReportType.EQUITY: [
            "executive_summary", "financial_highlights", "fundamentals_deep_dive",
            "technicals", "holdings", "valuation_analysis",
            "sentiment_news", "risk_factors", "scenario_outlook",
        ],
        ReportType.GENERAL: [
            "executive_summary", "financial_highlights",
            "sentiment_news", "risk_factors", "scenario_outlook",
        ],
        ReportType.CUSTOM: [
            "executive_summary", "financial_highlights",
            "fundamentals_deep_dive", "valuation_analysis",
            "sentiment_news", "risk_factors", "scenario_outlook",
        ],
    }

    sections = config_sections or _defaults.get(report_type, _defaults[ReportType.GENERAL])

    lines = [
        f"This is a {report_type.value.upper()} report. "
        f"Include EXACTLY these sections in EXACTLY this order (120-180 words per text section):"
    ]
    for section_key in sections:
        heading = _SECTION_HEADINGS.get(section_key, f"## {section_key.replace('_', ' ').title()}")
        heading = heading.replace("{n}", str(outlook_label.split("-")[0]))
        instruction_fn = _SECTION_INSTRUCTION_MAP.get(section_key)
        instruction = instruction_fn(outlook_label, market_metrics=market_metrics, sentiment_findings=sentiment_findings) if instruction_fn else ""
        lines.append(f"\n{heading}\n{instruction}")

    return "\n".join(lines)


def _check_and_scrub_numeric_drift(markdown_body: str, market_metrics: MarketMetrics) -> str:
    """
    Deterministic check and enforcement for numeric fidelity.
    Searches markdown_body for currency patterns and scrubs unverified/hallucinated figures.
    """
    known_tokens = set()

    for k, v in market_metrics.model_dump().items():
        if isinstance(v, str):
            known_tokens.add(v.strip())
            bare = re.sub(r"^[^\d]+", "", v.strip())
            if bare:
                known_tokens.add(bare)
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            known_tokens.add(f"{v:.2f}")
            known_tokens.add(str(v))
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    for subk, subv in item.items():
                        if isinstance(subv, str):
                            known_tokens.add(subv.strip())
                        elif isinstance(subv, (int, float)) and not isinstance(subv, bool):
                            known_tokens.add(f"{subv:.2f}")
        elif isinstance(v, dict):
            for subk, subv in v.items():
                if isinstance(subv, (int, float)) and not isinstance(subv, bool):
                    known_tokens.add(f"{subv:.2f}")
                elif isinstance(subv, str):
                    known_tokens.add(subv.strip())

    currency_matches = re.findall(r"(?:Rs\.?|₹|\$)\s*\d[\d,]*\.\d{2}", markdown_body)
    for match in set(currency_matches):
        is_known = False
        clean_match = match.strip()
        bare_num = re.sub(r"^[^\d]+", "", clean_match).replace(",", "")
        try:
            val_f = float(bare_num)
        except ValueError:
            val_f = None

        for token in known_tokens:
            if clean_match in token or token in clean_match:
                is_known = True
                break
            if val_f is not None and f"{val_f:.2f}" in token:
                is_known = True
                break

        if not is_known:
            logger.warning(
                "Potential numeric drift in Chief Editor Markdown: scrubbing unverified figure '%s'",
                match
            )
            markdown_body = markdown_body.replace(match, "[figure unavailable — not independently verified]")

    return markdown_body


def compute_average_score(score_results: list) -> float:
    if not score_results:
        return 0.0
    scored = [r for r in score_results if r.score_value is not None]
    if not scored:
        return 0.0
    return sum(r.score_value for r in scored) / len(scored)


def run_chief_editor(
    market_metrics: MarketMetrics,
    sentiment_findings: SentimentFindings,
    report_type: ReportType = ReportType.GENERAL,
    report_spec: Optional[ReportSpec] = None,
    editorial_goal: Optional[str] = None,
    aml_result: Optional[AMLScreeningResult] = None,
    score_results: Optional[list] = None,
) -> str:
    """Synthesize validated market data + sentiment findings into the final report Markdown."""
    client = genai.Client(api_key=settings.gemini_api_key)
    prompt_name = "credit_editor" if score_results else "chief_editor"
    system_prompt = load_agent_prompt(prompt_name)

    effective_goal = editorial_goal or (report_spec.editorial_goal if report_spec else None)
    outlook_label = f"{market_metrics.outlook_months}-Month"
    section_instruction = _build_section_instructions(
        report_type,
        outlook_label,
        market_metrics=market_metrics,
        report_spec=report_spec,
        sentiment_findings=sentiment_findings,
        editorial_goal=effective_goal,
    )

    custom_metrics_block = ""
    if market_metrics.custom_metrics:
        import json
        custom_metrics_block = f"\n\nCUSTOM COMPUTED METRICS (JSON):\n{json.dumps(market_metrics.custom_metrics, indent=2)}"

    aml_context_block = ""
    if aml_result:
        elevated_findings = [f for f in aml_result.findings if f.severity in (AMLSeverity.HIGH, AMLSeverity.ELEVATED)]
        if elevated_findings:
            items_str = "\n".join(f"- [{f.severity.value}] {f.source_name}: {f.finding_summary}" for f in elevated_findings)
            aml_context_block = (
                f"\n\nAML / COMPLIANCE SCREENING FINDINGS (ELEVATED FLAGS DETECTED):\n{items_str}\n\n"
                "MANDATORY CONSISTENCY RULE FOR CHIEF EDITOR:\n"
                "- Compliance screening flagged the elevated items listed above.\n"
                "- If your narrative touches on risk factors or compliance, you MUST accurately reflect that these potential flags exist.\n"
                "- NEVER state that there are 'no adverse compliance flags', 'no direct exposure', or a 'clean record' when elevated flags are present!"
            )
        else:
            aml_context_block = (
                "\n\nAML / COMPLIANCE SCREENING FINDINGS: Clean across all primary registries (OFAC, UN, EU, World Bank, SEC, Adverse Media).\n"
                "MANDATORY CONSISTENCY RULE FOR CHIEF EDITOR:\n"
                "- You may state that automated screening identified no adverse sanctions, debarment, or regulatory enforcement flags."
            )

    score_context_block = ""
    if market_metrics.custom_metrics and market_metrics.custom_metrics.get("credit_scoring_unavailable"):
        score_context_block = (
            "\n\nCREDIT SCORING RESULTS:\n"
            "MANDATORY RULE: Credit scoring could not be fully completed for this entity due to unavailable or un-parsable source data (e.g., missing annual report).\n"
            "If a 'Credit Scoring Summary' subsection is included, you MUST explicitly state that the scoring was unavailable and do NOT invent any scores."
        )
    elif market_metrics.custom_metrics and market_metrics.custom_metrics.get("banking_not_applicable"):
        # Clean 3-pillar score — Banking is N/A, not missing
        scoring_results = [r for r in score_results if r.score_value is not None]
        avg_score = compute_average_score(scoring_results) if scoring_results else 0.0
        score_context_block = f"\n\nCREDIT SCORING RESULTS (Average Score: {avg_score:.1f}/100):\n"
        for res in score_results:
            if res.not_applicable_reason:
                score_context_block += f"- {res.score_category.value}: N/A — {res.not_applicable_reason}\n"
            else:
                comfort = res.comforts[0].claim if res.comforts else "N/A"
                discomfort = res.discomforts[0].claim if res.discomforts else "N/A"
                score_context_block += f"- {res.score_category.value}: {res.score_value:.0f}/100\n  Strength: {comfort}\n  Weakness: {discomfort}\n"
        score_context_block += (
            "\nBanking Score: N/A — entity does not maintain conventional bank credit facilities.\n"
            "\nMANDATORY RULE: Include a 'Credit Scoring Summary' subsection. State the Banking N/A status explicitly."
        )
    elif score_results:
        avg_score = compute_average_score(score_results)
        score_context_block = f"\n\nCREDIT SCORING RESULTS (Average Score: {avg_score:.1f}/100):\n"
        for res in score_results:
            comfort = res.comforts[0].claim if res.comforts else "N/A"
            discomfort = res.discomforts[0].claim if res.discomforts else "N/A"
            score_context_block += f"- {res.score_category.value}: {res.score_value:.0f}/100\n  Strength: {comfort}\n  Weakness: {discomfort}\n"
        score_context_block += "\nMANDATORY RULE: If credit scoring results are provided, you MUST explicitly include a 'Credit Scoring Summary' subsection synthesizing these scores and the average."

    user_message = (
        f"Report type: {report_type.value}\n"
        f"Editorial Goal: {effective_goal or 'Standard Comprehensive Financial Review'}\n"
        f"Outlook window: {outlook_label}\n\n"
        f"{section_instruction}\n\n"
        "Compile the final report from the following already-verified data. "
        "Do not invent or alter any number — every figure below has already "
        "been validated; state a field as unavailable if it's listed in "
        "unavailable_fields, rather than guessing a value for it.\n\n"
        f"MARKET METRICS (JSON):\n{market_metrics.model_dump_json(indent=2)}\n\n"
        f"SENTIMENT FINDINGS (JSON):\n{sentiment_findings.model_dump_json(indent=2)}"
        f"{custom_metrics_block}"
        f"{score_context_block}"
        f"{aml_context_block}\n"
    )

    response = generate_with_retry(
        client,
        model=settings.gemini_model,
        contents=[types.Content(role="user", parts=[types.Part(text=user_message)])],
        config=types.GenerateContentConfig(system_instruction=system_prompt),
    )

    markdown_body = (response.text or "").strip()
    if not markdown_body:
        raise ValueError("Chief Editor returned empty output — check the model response/safety filters")

    markdown_body = _check_and_scrub_numeric_drift(markdown_body, market_metrics)

    logger.info("Chief Editor produced %d characters of Markdown", len(markdown_body))
    return markdown_body


def _finding_sort_key(finding: AMLFinding) -> tuple[int, str, str]:
    """
    Sort key for AML findings:
      1. High severity (🔴 High)
      2. Elevated severity (🟠 Elevated)
      3. Watch severity with actual substantive content (🟡 Watch)
      4. Clear / No match findings (🟢 None)
      5. Fetch errors / Network failures pushed to the bottom
    """
    summary_l = finding.finding_summary.lower()
    is_failure = any(
        kw in summary_l
        for kw in (
            "could not be completed",
            "could not fetch",
            "screener error",
            "401 client error",
            "404 client error",
            "unauthorized",
            "manual check recommended",
        )
    ) and not any(kw in summary_l for kw in ("name match found", "potential match", "name string found"))

    if is_failure:
        priority = 50
    elif finding.severity.value == "High":
        priority = 10
    elif finding.severity.value == "Elevated":
        priority = 20
    elif finding.severity.value == "Watch":
        priority = 30
    else:  # "None"
        priority = 40

    return (priority, finding.entity_screened, finding.source_name)


def render_aml_markdown(aml_result: AMLScreeningResult) -> str:
    """
    Render the AML/ABC screening result as a Markdown section.
    Called separately from run_chief_editor — AML content is never passed
    through the LLM; it is formatted deterministically from validated data.
    This prevents the model from paraphrasing or altering screening findings.
    """
    has_high = any(f.severity == AMLSeverity.HIGH for f in aml_result.findings)
    has_elevated = any(f.severity == AMLSeverity.ELEVATED for f in aml_result.findings)

    if has_high:
        status_banner = "> 🔴 **CRITICAL COMPLIANCE NOTICE:** Confirmed match(es) or high-severity flags identified in global sanctions/debarment registries. Immediate manual verification required.\n"
    elif has_elevated:
        status_banner = "> 🟠 **ELEVATED COMPLIANCE ADVISORY:** Potential name match(es) or regulatory enforcement records detected. Compliance officer review recommended.\n"
    else:
        status_banner = "> 🟢 **COMPLIANCE SCREENING STATUS:** Automated multi-registry screening complete. No confirmed sanctions, debarments, or adverse enforcement records identified.\n"

    lines: list[str] = [
        "---",
        "",
        "# AML / ABC Compliance Screening",
        "",
        status_banner,
        f"**Entities screened:** {', '.join(aml_result.entities_screened) or 'None'}  ",
        f"**Screened at:** {aml_result.screened_at.isoformat()}  ",
        "",
        "| Entity Screened | Source | Finding | Severity | Citation |",
        "|---|---|---|---|---|",
    ]

    severity_icons = {
        "None":     "🟢 None",
        "Watch":    "🟡 Watch",
        "Elevated": "🟠 Elevated",
        "High":     "🔴 High",
    }

    # Sort findings so critical hits appear first, clean results in the middle
    sorted_findings = sorted(aml_result.findings, key=_finding_sort_key)

    for finding in sorted_findings:
        icon = severity_icons.get(finding.severity.value, finding.severity.value)
        citation = f"[Link]({finding.source_url})" if finding.source_url else "—"
        lines.append(
            f"| {finding.entity_screened} "
            f"| {finding.source_name} "
            f"| {finding.finding_summary} "
            f"| {icon} "
            f"| {citation} |"
        )

    if not aml_result.findings:
        lines.append("| — | — | No findings generated | — | — |")

    lines += [
        "",
        "> **Compliance Disclaimer:** " + aml_result.disclaimer,
        "",
    ]
    return "\n".join(lines)




def render_credit_scoring_markdown(score_results: list) -> str:
    """
    Deterministic (no-LLM) Markdown table for the 4-pillar credit scoring scorecard.
    Renders whatever categories are present — does not require all 4.
    Handles Banking N/A gracefully.
    """
    if not score_results:
        return ""
    lines = ["## Credit Scoring & Governance Scorecard", "", "| Category | Score | Verdict |", "|---|---|---|"]
    for r in score_results:
        if r.not_applicable_reason:
            lines.append(f"| {r.score_category.value} | N/A | {r.not_applicable_reason} |")
        else:
            comfort = r.comforts[0].claim if r.comforts else ""
            discomfort = r.discomforts[0].claim if r.discomforts else ""
            verdict = comfort or discomfort or (r.raw_evidence_snippets[:150] if r.raw_evidence_snippets else "")
            score_val = f"{r.score_value:.0f}/100" if r.score_value is not None else "N/A"
            lines.append(f"| {r.score_category.value} | {score_val} | {verdict} |")
    scored = [r for r in score_results if r.score_value is not None]
    if scored:
        avg = sum(r.score_value for r in scored) / len(scored)
        lines.append("")
        lines.append(f"**Average score: {avg:.1f}/100** across {len(scored)} of 4 pillars evaluated.")
    na_count = sum(1 for r in score_results if r.not_applicable_reason)
    unevaluated = 4 - len(score_results)
    if unevaluated > 0:
        missing = {"Finances", "Business & Management", "Hygiene", "Banking"} - {r.score_category.value for r in score_results}
        lines.append(f"*Not scored this run: {', '.join(sorted(missing))}.*")
    if na_count > 0:
        lines.append(f"*{na_count} pillar(s) marked N/A (not applicable to this entity).*")
    return "\n".join(lines)
