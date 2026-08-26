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
