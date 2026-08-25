"""
Centralized configuration for the Financial Report Generator.

Every other module reads settings from here rather than calling
os.environ directly — one place to see the full config surface, and
pydantic-settings validates types/required fields at startup instead of
failing deep inside a pipeline run.
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- API Keys (required — will raise at startup if missing, which is
    # the point: fail fast before burning any API calls) ---
    gemini_api_key: str
    tavily_api_key: str

    # --- Model ---
    gemini_model: str = "gemini-3.6-flash"

    # --- PDF ---
    pdf_engine: str = "weasyprint"  # "weasyprint" | "xhtml2pdf"

    # --- Paths ---
    output_dir: Path = Path("outputs")
    cache_dir: Path = Path("cache")
    templates_dir: Path = Path("templates")
    static_dir: Path = Path("static")
    agents_dir: Path = Path("agents")

    # --- Cache ---
    cache_ttl_hours: int = 6

    # --- Outlook window ---
    outlook_months: int = 6

    # --- DSH (DeepSeek Harness) Runtime ---
    dsh_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    dsh_model: str = "gemini-2.5-flash"
    dsh_max_tokens: int = 49152
    dsh_cordis_config: str = "cordis.yml"
    mcp_server_host: str = "127.0.0.1"
    mcp_server_port: int = 8000

    def ensure_dirs(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
