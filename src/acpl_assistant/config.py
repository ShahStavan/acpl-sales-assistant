"""Runtime configuration read from the environment (see ``.env.example``).

Settings: ``PORT``, ``LLM_PROVIDER``, ``LLM_MODEL``, ``LLM_FALLBACK_MODELS``,
``LLM_BREAKER_THRESHOLD``, ``LLM_BREAKER_COOLDOWN_S``, ``LLM_API_KEY``, ``LLM_BASE_URL``,
``ACPL_DATA_DIR``, ``ACPL_WAREHOUSE``, ``LOG_LEVEL``. The service never reads the OpenCode
build token; the LLM key is the operator's own. DESIGN.md §3.6.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _repo_root() -> Path:
    """Return the repository root (where ``prepare.py`` lives).

    Resolved from this file's location so every path in the settings works
    regardless of the caller's ``cwd``.
    """
    return Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Application settings sourced from environment variables (or ``.env``)."""

    model_config = SettingsConfigDict(
        env_file=_repo_root() / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- service ------------------------------------------------------------
    PORT: int = Field(default=8000, ge=1, le=65535)
    LOG_LEVEL: str = "INFO"

    # --- data ---------------------------------------------------------------
    ACPL_DATA_DIR: str = "data/fmcg-sales-copilot-ai-engineer-mid-4to6"
    ACPL_WAREHOUSE: str = "warehouse.duckdb"

    # --- LLM provider -------------------------------------------------------
    LLM_PROVIDER: str = "gemini"
    LLM_MODEL: str = "gemini-2.5-flash"
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = ""
    LLM_TIMEOUT_S: int = Field(default=30, ge=1)

    # --- provider fallback and circuit breaker ------------------------------
    # Comma-separated, tried in order after LLM_MODEL when a model is unavailable.
    LLM_FALLBACK_MODELS: str = ""
    LLM_BREAKER_THRESHOLD: int = Field(default=2, ge=1)
    LLM_BREAKER_COOLDOWN_S: int = Field(default=300, ge=0)

    @property
    def llm_model_chain(self) -> tuple[str, ...]:
        """``LLM_MODEL`` followed by each distinct fallback, in the order configured.

        Order is the whole contract: the first entry is what a healthy request is answered
        and priced by, and every later entry is a degradation the caller should be able to
        see in the response. Duplicates are dropped so a fallback list that repeats the
        primary does not make the same exhausted model be tried twice.
        """
        chain: list[str] = []
        for name in [self.LLM_MODEL, *self.LLM_FALLBACK_MODELS.split(",")]:
            model = name.strip()
            if model and model not in chain:
                chain.append(model)
        return tuple(chain)

    @property
    def acpl_data_dir_resolved(self) -> Path:
        """Absolute path to the ACPL data directory."""
        return _repo_root() / self.ACPL_DATA_DIR

    @property
    def acpl_warehouse_resolved(self) -> Path:
        """Absolute path to the DuckDB warehouse file."""
        return _repo_root() / self.ACPL_WAREHOUSE


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton settings instance (cached after first call)."""
    return Settings()
