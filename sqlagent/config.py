"""Runtime settings (env vars prefixed SQLAGENT_, or a .env file)."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root holding config/ and data/. Override with SQLAGENT_HOME when installed as a package.
ROOT = Path(os.environ.get("SQLAGENT_HOME", Path(__file__).resolve().parents[1]))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SQLAGENT_", env_file=".env", extra="ignore")

    warehouse_path: Path = ROOT / "data" / "warehouse.duckdb"
    catalog_path: Path = ROOT / "config" / "catalog.yaml"
    glossary_path: Path = ROOT / "config" / "glossary.yaml"
    policy_path: Path = ROOT / "config" / "policy.yaml"
    examples_path: Path = ROOT / "config" / "examples.yaml"
    audit_path: Path = ROOT / "data" / "audit.jsonl"
    checkpoint_path: Path = ROOT / "data" / "checkpoints.sqlite"

    # LLM: "ollama" (default, free/local), "anthropic", "openai" (any OpenAI-compatible, e.g. Groq)
    llm_provider: str = "ollama"
    llm_model: str = "qwen2.5-coder:7b"
    llm_base_url: str | None = None
    llm_temperature: float = 0.0
    llm_timeout_s: float = 120.0

    max_sql_attempts: int = 3
    max_clarification_rounds: int = 2
    retrieval_top_k_tables: int = 6
    max_estimated_rows: int = 50_000_000

    langfuse_enabled: bool = False
    # Dev mode exposes the demo identities from config/users.yaml to the web UI's user picker.
    # Set SQLAGENT_DEV_MODE=false in production, where identity comes from your SSO.
    dev_mode: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
