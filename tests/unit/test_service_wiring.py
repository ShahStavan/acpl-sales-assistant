"""The process-level wiring: what is built once, when, and what happens without a warehouse.

Three things are shared for the life of the process, and each of them is expensive enough
that building it per request would show up in the latency the endpoint reports. These tests
hold that they are built once, built lazily, and released at shutdown — and that a process
started without a warehouse still comes up and says so.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from acpl_assistant import service
from acpl_assistant.config import Settings


@pytest.fixture(autouse=True)
def _clean_caches() -> Any:
    """Leave the process-wide caches as they were found.

    They are module state; a test that fills one would otherwise hand the next test a
    warehouse or a provider client it never asked for.
    """
    service.get_client.cache_clear()
    service.get_vocabulary.cache_clear()
    yield
    service.get_client.cache_clear()
    service.get_vocabulary.cache_clear()


class TestSharedClient:
    def test_it_is_built_once_and_reused(self) -> None:
        """A connection opened per call would land in the latency the endpoint reports."""
        assert service.get_client() is service.get_client()

    def test_nothing_is_built_before_it_is_asked_for(self) -> None:
        assert service.get_client.cache_info().currsize == 0

    def test_shutdown_releases_a_client_that_was_built(self) -> None:
        client = service.get_client()
        with TestClient(service.app):
            pass
        assert client._client.is_closed
        assert service.get_client.cache_info().currsize == 0

    def test_shutdown_does_not_build_one_that_was_never_needed(self) -> None:
        """A process serving only /health and /actions never touches the provider."""
        with TestClient(service.app):
            pass
        assert service.get_client.cache_info().currsize == 0


class TestSharedVocabulary:
    def test_it_is_read_once_per_warehouse(self, prepared: tuple[Path, dict]) -> None:
        db_path, _ = prepared
        first = service.get_vocabulary(db_path)
        assert service.get_vocabulary(db_path) is first
        assert first.of("brands")

    def test_the_dependency_wrapper_returns_it(self, prepared: tuple[Path, dict]) -> None:
        db_path, _ = prepared
        settings = Settings(ACPL_WAREHOUSE=str(db_path))
        assert service.get_vocab(settings) is service.get_vocabulary(db_path)

    def test_a_missing_warehouse_is_unavailable_rather_than_broken(self, tmp_path: Path) -> None:
        settings = Settings(ACPL_WAREHOUSE=str(tmp_path / "never-built.duckdb"))
        with pytest.raises(HTTPException) as caught:
            service.get_vocab(settings)
        assert caught.value.status_code == 503
        assert "prepare.py" in caught.value.detail


class TestEntryPoint:
    def test_it_serves_the_app_on_the_configured_port(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``main`` is one call to uvicorn; what is asserted is what it passes."""
        captured: dict[str, Any] = {}

        class FakeUvicorn:
            @staticmethod
            def run(app: Any, **kwargs: Any) -> None:
                captured["app"] = app
                captured.update(kwargs)

        monkeypatch.setitem(__import__("sys").modules, "uvicorn", FakeUvicorn)
        service.main()
        assert captured["app"] is service.app
        assert captured["port"] == service.get_settings().PORT
        assert captured["host"] == "0.0.0.0"
        assert captured["log_level"] == service.get_settings().LOG_LEVEL.lower()
