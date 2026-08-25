"""
Lightweight loader for agent system prompt files in agents/*.md.

Agent .md files carry a small YAML frontmatter block for documentation/metadata
and a prose body below it for the actual system prompt text.
`load_agent_prompt` parses the file and extracts the body text at runtime.
"""
from __future__ import annotations

from typing import Any

import yaml

from config import settings

_FRONTMATTER_DELIM = "---"


def _split_frontmatter(raw_text: str) -> tuple[dict[str, Any], str]:
    """
    Split a Markdown file of the form:

        ---
        key: value
        ---
        # Body starts here...

    into (frontmatter_dict, body_text). Returns ({}, raw_text) unchanged
    if the file has no frontmatter block.
    """
    stripped = raw_text.lstrip()
    if not stripped.startswith(_FRONTMATTER_DELIM):
        return {}, raw_text

    parts = stripped.split(_FRONTMATTER_DELIM, 2)
    if len(parts) < 3:
        # Malformed (opening delimiter with no closing one) — treat the
        # whole thing as body rather than silently dropping content.
        return {}, raw_text

    _, frontmatter_raw, body = parts
    frontmatter = yaml.safe_load(frontmatter_raw) or {}
    if not isinstance(frontmatter, dict):
        raise ValueError(f"Frontmatter did not parse to a dict: {frontmatter_raw!r}")

    return frontmatter, body.strip()


def load_agent_prompt(name: str) -> str:
    """Load the system-instruction body text for agents/{name}.md."""
    path = settings.agents_dir / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Agent prompt not found: {path}")

    _, body = _split_frontmatter(path.read_text(encoding="utf-8"))
    if not body:
        raise ValueError(f"Agent prompt file has no body content: {path}")
    return body
