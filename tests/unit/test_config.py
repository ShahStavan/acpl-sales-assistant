"""Tests for ``config.py``: defaults, env override, and missing-key case."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import SettingsConfigDict
import pytest

from acpl_assistant.config import Settings, _repo_root, get_settings

_ENV_KEYS = [
    "PORT",
    "LOG_LEVEL",
    "ACPL_DATA_DIR",
    "ACPL_WAREHOUSE",
    "LLM_PROVIDER",
    "LLM_MODEL",
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_TIMEOUT_S",
    "LLM_FALLBACK_MODELS",
    "LLM_BREAKER_THRESHOLD",
    "LLM_BREAKER_COOLDOWN_S",
]


def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Delete every env var that ``Settings`` reads and neutralise the ``.env`` file."""
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(
        Settings,
        "model_config",
        SettingsConfigDict(
            env_file=None,
            env_file_encoding="utf-8",
            extra="ignore",
        ),
    )


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_env(monkeypatch)
    s = Settings()
    assert s.PORT == 8000
    assert s.LOG_LEVEL == "INFO"
    assert s.ACPL_DATA_DIR == "data/fmcg-sales-copilot-ai-engineer-mid-4to6"
    assert s.ACPL_WAREHOUSE == "warehouse.duckdb"
    assert s.LLM_PROVIDER == "gemini"
    assert s.LLM_MODEL == "gemini-2.5-flash"
    assert s.LLM_API_KEY == ""
    assert s.LLM_BASE_URL == ""
    assert s.LLM_TIMEOUT_S == 30
    assert s.LLM_FALLBACK_MODELS == ""
    assert s.LLM_BREAKER_THRESHOLD == 2
    assert s.LLM_BREAKER_COOLDOWN_S == 300


def test_the_chain_is_the_primary_alone_when_no_fallback_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_env(monkeypatch)
    assert Settings().llm_model_chain == ("gemini-2.5-flash",)


def test_the_chain_follows_the_configured_order(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_env(monkeypatch)
    monkeypatch.setenv("LLM_MODEL", "gemini-2.5-flash")
    monkeypatch.setenv("LLM_FALLBACK_MODELS", "gemini-2.5-flash-lite, gemini-3.1-flash-lite")
    assert Settings().llm_model_chain == (
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-3.1-flash-lite",
    )


def test_the_chain_drops_blanks_and_repeats(monkeypatch: pytest.MonkeyPatch) -> None:
    """A list that names the primary again must not make an exhausted model be tried twice."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("LLM_MODEL", "gemini-2.5-flash")
    monkeypatch.setenv("LLM_FALLBACK_MODELS", " , gemini-2.5-flash ,gemini-3.1-flash-lite,, ")
    assert Settings().llm_model_chain == ("gemini-2.5-flash", "gemini-3.1-flash-lite")


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_env(monkeypatch)
    monkeypatch.setenv("PORT", "9000")
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_MODEL", "gpt-4o")
    monkeypatch.setenv("LLM_TIMEOUT_S", "60")
    monkeypatch.setenv("ACPL_DATA_DIR", "/custom/data")

    s = Settings()
    assert s.PORT == 9000
    assert s.LLM_PROVIDER == "openai"
    assert s.LLM_MODEL == "gpt-4o"
    assert s.LLM_TIMEOUT_S == 60
    assert s.ACPL_DATA_DIR == "/custom/data"


def test_missing_api_key_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_env(monkeypatch)
    s = Settings()
    assert s.LLM_API_KEY == ""
    assert isinstance(s.LLM_API_KEY, str)


def test_paths_resolve_to_repo_root(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_env(monkeypatch)
    root = _repo_root()
    assert root.joinpath("prepare.py").is_file()
    assert root.joinpath("pyproject.toml").is_file()

    s = Settings()
    assert s.acpl_data_dir_resolved == root / "data/fmcg-sales-copilot-ai-engineer-mid-4to6"
    assert s.acpl_warehouse_resolved == root / "warehouse.duckdb"
    assert isinstance(s.acpl_data_dir_resolved, Path)
    assert isinstance(s.acpl_warehouse_resolved, Path)


def test_get_settings_is_cached() -> None:
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2
