import re
from pathlib import Path

path = Path("harness/mcp_server.py")
content = path.read_text(encoding="utf-8")

# 1. Replace ticker assignments
content = content.replace(
    'ticker = state.ticker or arguments.get("ticker")',
    'ticker = state.ticker'
)

# 2. Replace query_or_ticker assignments
content = content.replace(
    'query_or_ticker = state.ticker or state.company_name or arguments.get("query_or_ticker") or ""',
    'query_or_ticker = state.ticker or state.company_name'
)
content = content.replace(
    'query_or_ticker = state.ticker or state.company_name or arguments.get("query_or_ticker")',
    'query_or_ticker = state.ticker or state.company_name'
)

# 3. Replace entity_name assignments
content = content.replace(
    'entity_name = state.company_name or arguments.get("entity_name") or "Tata Consultancy Services"',
    'entity_name = state.company_name or ""'
)
content = content.replace(
    'entity_name = state.company_name or arguments.get("entity_name") or ""',
    'entity_name = state.company_name or ""'
)

# 4. Remove ticker from required lists in TOOL_DEFINITIONS and NEW_TOOLS
# We'll use regex to remove "ticker" from arrays like ["ticker"] or ["expression", "ticker", "metric_name"]
content = re.sub(r'"required":\s*\["ticker"\]\s*,?', '', content)
content = content.replace('"expression", "ticker", "metric_name"', '"expression", "metric_name"')

# Remove "ticker" or "query_or_ticker" property lines
content = re.sub(r'\s*"ticker":\s*\{[^\}]+\},?', '', content)
content = re.sub(r'\s*"query_or_ticker":\s*\{[^\}]+\},?', '', content)

path.write_text(content, encoding="utf-8")
print("Replacements complete!")
