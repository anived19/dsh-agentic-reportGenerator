# Deployment Guide: Finoscale Agentic Report Generator

This document outlines the deployment and configuration steps for the updated `dsh-agentic-reportGenerator`, now including the Subagent Credit Scoring and Analyst Gate features.

## Prerequisites

- Node.js (v18+ recommended)
- Python 3.10+
- `pip install -r requirements.txt`
- `npm install` (to install DSH locally if applicable)

## Configuration

Copy `.env.example` to `.env` and fill in the required values:
- `GEMINI_API_KEY`: Required for LLM inference (Gemini 3.5 Flash Lite).
- `TAVILY_API_KEY`: Required for web search and adverse media.
- `DSH_TELEMETRY_DISABLED`: MUST be set to `1` in the environment before running. The `dsh_driver.py` enforces a strict lockdown.

## Execution Model

The system uses a 3-layer architecture:
1. **Layer 1: Pre-Analysis & Scrapers**: Tools fetch data and OCR PDFs (Pymupdf4llm).
2. **Layer 2: DSH Native Orchestration**: The DSH framework runs the main ReAct loop and handles MCP tools.
   - For credit scoring, the agent uses DSH subagents sequentially (locked via Python-side mutexes).
   - An Analyst Review Gate interrupts the process when credit scoring drafts are generated.
3. **Layer 3: Grounded Synthesis**: Generates Markdown and PDF.

## Running the Pipeline

You can run the pipeline directly:
```bash
python -m harness.dsh_driver --ticker TCS.NS --report-type equity
```

### Analyst Review Gate
When the main agent reaches the Analyst Review phase (if credit scoring is run), it will write to `analyst_review_pending.json` and suspend. The `dsh_driver` will prompt you via stdout/stdin to approve or reject the draft. Once approved, the driver will inject the response back into DSH, and the agent will proceed to finalize the report.

## Subagent Lock Limitations

To avoid severe rate limits and context window bloat, the subagents for credit scoring (Finances, Business & Management, Hygiene, Banking) are enforced to run strictly sequentially. The MCP server provides explicit mutex lock methods (`mcp__finoscale__get_category_text` and `mcp__finoscale__submit_category_result`) to manage this.

## Testing the Fallback Scoring Path

To deterministically test the subagent credit scoring path without relying on a successful annual report download (which can fail due to search engine limitations or missing files), you can force the fallback data dossier to generate by setting an environment variable:

```bash
set FORCE_ANNUAL_REPORT_NOT_FOUND=1
python -m harness.dsh_driver --ticker TCS.NS --report-type equity
```

This will automatically short-circuit `fetch_annual_report`, trigger `build_fallback_dossier` during the `build_section_index` step, and spawn all 4 subagents sequentially using live session data (market, sentiment, AML) instead of PDF excerpts. Verify the final session telemetry to ensure `credit_scoring_source` is set to `fallback_market_data`.

## Clean-Context Verification Canary

To verify that context isolation works correctly and subagents are not leaking instructions or past state from the main orchestrator loop, you can run a canary test:

1. Enable the context debug hook by setting the environment variable DSH_SUBAGENT_CONTEXT_DEBUG=1.
2. Run the agent. During the session, inject a unique marker string (e.g., CANARY_MARKER_12345) into the agent's memory (for example, by having it fetch a mocked web page, or by editing the orchestrator prompt).
3. Check the written debug files (subagent_context_<Category>.json) generated during execution.
4. If the marker string appears in the context JSON, then context isolation is broken. If it does not appear, then clean-context spawning is successfully preventing prompt leakage.
