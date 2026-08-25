# Glossary

Plain-English definitions of every agentic and technical concept used in this codebase.
Written for someone who knows Python but has not built with LLM-based agent frameworks before.

---

## A

### Agent
A program where an LLM decides what to do next at runtime, not just at design time. In this codebase, the **DeepSeek Harness (DSH)** orchestrator operates as the primary autonomous agent: it reasons about the user's intent, inspects gathered data, queries deterministic MCP tools, validates findings, and structures the report before handoff.

### Agent Prompt / System Instruction
The text in `agents/*.md` (or `cordis.yml` for DSH) that defines the agent's standing persona, rules, and operational constraints. For the Chief Editor (`agents/chief_editor.md`), it instructs: "only write numbers directly from the JSON empirical data; never invent or calculate figures."

---

## B

### Bounded Execution / Max Turns
A hard ceiling on the number of reasoning/action steps in an agent run (configured in `AgentState.max_turns = 20`). "Bounded" is what separates a responsible agentic design from one that can spiral into infinite API loops.

---

## C

### Chief Editor
The single-shot LLM call in `harness/synthesis.py` that takes empirical market data + news sentiment findings and produces the final Markdown report. It is deliberately **not** agentic: no tools, no loop, no decision about what to query next. By the time it runs, all data points are already fetched and validated. Its job is formatting and synthesis only.

### CitedClaim
The Pydantic model in `schemas.py` that enforces the citation discipline: every sentiment claim must have a valid `source_url`. If a claim has an invalid or missing URL, Pydantic validation flags the record before it reaches the PDF.

### Model Context Protocol (MCP) Server
The stateful tool execution server in `harness/mcp_server.py`. Exposes all 22 data fetching, calculation, search, AML screening, and validation tools over stdio JSON-RPC to the DSH agent runtime.

---

## D

### Deterministic
A stage where the code always produces the exact same output for the same input, with zero LLM guesswork. `tools/finance_tools.py` (yfinance metrics), `tools/peer_resolver.py` (peer discovery), `tools/pdf_tools.py` (HTML → PDF), and `tools/aml_tools.py` (sanctions matching) are all strictly deterministic.

---

## F

### FinalReport
The Pydantic model that carries everything the PDF renderer needs: the ticker, company name, the Chief Editor's Markdown body, the validated `MarketMetrics`, the `SentimentFindings`, and optionally an `AMLScreeningResult`. It's a schema, not a class with methods — once assembled, it is passed to `compile_pdf()` and never modified.

### Frontmatter
The YAML block between `---` delimiters at the top of `.md` files in `agents/`. Loaded and parsed at runtime by `harness/md_loader.py:_split_frontmatter()` to extract system prompt text.

---

## H

### Harness
The shared execution layer in the `harness/` directory (`dsh_driver.py`, `mcp_server.py`, `intake.py`, `synthesis.py`). It manages DSH process lifecycle, stdio MCP tool dispatch, interactive disambiguation IPC, and Stage 3 synthesis.

---

## M

### MarketMetrics
The Pydantic model that holds all fetched market data for one ticker — price, moving averages, RSI, MACD, volume trend, valuation multiples, earnings, analyst ratings, holdings, quarterly financials, and specialized sector metrics. Populated deterministically by `tools/finance_tools.py` and passed read-only to synthesis and PDF compilation.

---

## P

### Progressive Disclosure
The principle that an agent (or IDE) should only load the context it currently needs, not everything at once. The `.agents/skills/*.md` files follow this: they are discovered by the IDE but only loaded into context when the skill's trigger condition matches the current task.

---

## R

### ReAct (Reason + Act)
The autonomous multi-turn reasoning loop executed by DeepSeek Harness (DSH): `Perceive → Reason → Act (Call MCP Tool) → Observe (Inspect Result)`. DSH decides which tools to call, whether data is sufficient, and when to finalize.

### render_config.yaml
The configuration defining default section inclusions and ordering across report types (`sentiment`, `valuation`, `equity`, `general`). Can be overridden dynamically by DSH via `plan_report_format`.

---

## S

### SentimentFindings
The Pydantic model representing synthesized news sentiment: an `overall_sentiment` label (Bullish / Bearish / Neutral), a summary narrative, and lists of `CitedClaim` objects for catalysts and downside risks.

### Single-Shot Call
A Gemini `generate_content` call with no tools and no follow-up — one request, one response. Intake classification and the Chief Editor synthesis are both single-shot calls.

---

## U

### unavailable_fields
A list on `MarketMetrics` that records every field yfinance couldn't supply for a given ticker. When the Chief Editor receives this list, it is instructed to write "data unavailable" for those fields rather than guessing or omitting them.
