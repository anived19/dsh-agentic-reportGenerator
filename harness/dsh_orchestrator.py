"""
DSH Orchestrator Entry Point (Forwarder to True DSH Driver).

Forwards execution to harness.dsh_driver which executes DeepSeek Harness (DSH)
via its native Cordis plugin composition and MCP tool server.
"""
from __future__ import annotations

from harness.dsh_driver import default_ask_user, run_dsh_orchestrator

__all__ = ["run_dsh_orchestrator", "default_ask_user"]
