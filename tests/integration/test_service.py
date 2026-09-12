"""The HTTP surface: ``POST /actions`` and ``GET /health`` over the prepared warehouse.

Exercised through ``TestClient``, with no live LLM key and no network. Neither endpoint
calls a provider — the actions engine is code only and ``/health`` reports configuration —
so everything here is verifiable without spending a token. ``/ask`` needs a provider double
and lives in ``test_ask.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import duckdb
from fastapi.testclient import TestClient
import pytest

from acpl_assistant import service
from acpl_assistant.actions.engine import run_actions
from acpl_assistant.config import Settings, get_settings
from acpl_assistant.schemas import ActionItem
from acpl_assistant.service import app

pytestmark = pytest.mark.integration

EXPECTED_ITEMS = 55
STATES = {"RECOMMENDED", "PENDING_APPROVAL"}
CONTRACT_FIELDS = {"finding", "rule_id", "action", "state"}
EXTRA_FIELDS = {"period", "evidence", "priority"}


@pytest.fixture()
def client(prepared: tuple[Path, dict]) -> Iterator[TestClient]:
    """A client whose service reads the warehouse the session fixture prepared.

    The settings dependency is overridden rather than the environment patched, so the test
    never depends on — or disturbs — the developer's own ``.env`` or ``warehouse.duckdb``.
    """
    db_path, _ = prepared
    app.dependency_overrides[get_settings] = lambda: Settings(ACPL_WAREHOUSE=str(db_path))
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def unprepared_client(tmp_path: Path) -> Iterator[TestClient]:
    """A client pointed at a warehouse that was never built."""
    missing = tmp_path / "not-built.duckdb"
    app.dependency_overrides[get_settings] = lambda: Settings(ACPL_WAREHOUSE=str(missing))
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestActionsEndpoint:
    def test_all_returns_the_whole_ranked_list(self, client: TestClient) -> None:
        response = client.post("/actions", json={"scope": "all"})
        assert response.status_code == 200
        assert len(response.json()) == EXPECTED_ITEMS

    def test_every_item_matches_the_contract(self, client: TestClient) -> None:
        for item in client.post("/actions", json={"scope": "all"}).json():
            assert set(item) >= CONTRACT_FIELDS | EXTRA_FIELDS
            assert item["state"] in STATES
            ActionItem(**item)

    def test_a_region_narrows_the_list(self, client: TestClient) -> None:
        everything = client.post("/actions", json={"scope": "all"}).json()
        west = client.post("/actions", json={"scope": "West"}).json()
        assert 0 < len(west) < len(everything)

    def test_a_region_reads_the_same_however_it_is_written(self, client: TestClient) -> None:
        canonical = client.post("/actions", json={"scope": "West"}).json()
        for spelling in ("west", "WEST", "  west region "):
            assert client.post("/actions", json={"scope": spelling}).json() == canonical

    def test_an_unknown_scope_is_an_empty_list_not_an_error(self, client: TestClient) -> None:
        """A scope that resolves to no region is withheld, per DESIGN.md §5.5."""
        response = client.post("/actions", json={"scope": "Atlantis"})
        assert response.status_code == 200
        assert response.json() == []

    def test_a_missing_scope_is_rejected(self, client: TestClient) -> None:
        """An absent field is a client error; an unresolvable one is an empty result."""
        assert client.post("/actions", json={}).status_code == 422

    def test_a_non_string_scope_is_rejected(self, client: TestClient) -> None:
        assert client.post("/actions", json={"scope": ["West"]}).status_code == 422

    def test_priority_is_a_dense_rank_over_the_response(self, client: TestClient) -> None:
        items = client.post("/actions", json={"scope": "all"}).json()
        assert [i["priority"] for i in items] == list(range(1, len(items) + 1))

    def test_every_item_carries_traceable_evidence(self, client: TestClient) -> None:
        for item in client.post("/actions", json={"scope": "all"}).json():
            assert item["evidence"]
            assert all(row["source_file"] for row in item["evidence"])

    def test_the_gated_rules_are_gated_over_the_wire(self, client: TestClient) -> None:
        items = client.post("/actions", json={"scope": "all"}).json()
        gated = {i["rule_id"] for i in items if i["state"] == "PENDING_APPROVAL"}
        assert gated == {"R-01", "R-04", "R-08"}

    def test_two_identical_requests_return_identical_bodies(self, client: TestClient) -> None:
        first = client.post("/actions", json={"scope": "all"}).json()
        second = client.post("/actions", json={"scope": "all"}).json()
        assert first == second

    def test_it_reports_503_when_the_warehouse_is_not_built(
        self, unprepared_client: TestClient
    ) -> None:
        response = unprepared_client.post("/actions", json={"scope": "all"})
        assert response.status_code == 503
        assert "prepare.py" in response.json()["detail"]


class TestHealthEndpoint:
    def test_it_reports_ok_with_a_readable_warehouse(self, client: TestClient) -> None:
        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["warehouse"].endswith(".duckdb")
        assert body["model"]

    def test_it_reports_degraded_without_one(self, unprepared_client: TestClient) -> None:
        """The process is up but cannot serve: a probe must be able to tell the two apart."""
        response = unprepared_client.get("/health")
        assert response.status_code == 503
        assert response.json()["status"] == "degraded"

    def test_the_model_is_configuration_not_a_reachability_claim(self, client: TestClient) -> None:
        assert client.get("/health").json()["model"] == Settings().LLM_MODEL

    def test_it_publishes_the_fallback_chain(self, client: TestClient) -> None:
        """An operator reading a degraded run needs to know what it could have degraded to."""
        body = client.get("/health").json()
        assert body["fallback_models"] == list(Settings().llm_model_chain[1:])

    def test_no_model_is_degraded_before_a_provider_call_is_made(self, client: TestClient) -> None:
        """``degraded_models`` is observation, and a process that has not called out has
        observed nothing — reporting it must not be what first builds the client."""
        service.get_client.cache_clear()
        assert client.get("/health").json()["degraded_models"] == []
        assert service.get_client.cache_info().currsize == 0

    def test_an_open_breaker_shows_up_as_a_degraded_model(self, client: TestClient) -> None:
        service.get_client.cache_clear()
        try:
            service.get_client()._breaker.record_failure(Settings().LLM_MODEL)
            service.get_client()._breaker.record_failure(Settings().LLM_MODEL)
            assert client.get("/health").json()["degraded_models"] == [Settings().LLM_MODEL]
        finally:
            service.get_client.cache_clear()


class TestPublishedSurface:
    """The three routes, and nothing else. ``/ask`` is exercised in ``test_ask.py``, which
    substitutes a provider double; here it only has to be advertised.
    """

    def test_the_openapi_document_lists_exactly_the_contract(self, client: TestClient) -> None:
        paths = client.get("/openapi.json").json()["paths"]
        assert set(paths) == {"/ask", "/actions", "/health"}

    def test_ask_publishes_its_refusal_reason(self, client: TestClient) -> None:
        """``reason`` is the field the evaluation set asserts on, so it is part of the schema."""
        schema = client.get("/openapi.json").json()["components"]["schemas"]["AskResponse"]
        assert "reason" in schema["properties"]


class TestConcurrency:
    """A DuckDB connection is not safe to share across threads, and FastAPI runs sync
    endpoints in a thread pool. ``get_connection`` hands each request its own cursor; these
    tests hold that property rather than leaving it as a comment.
    """

    WORKERS = 8
    REQUESTS = 16

    def test_concurrent_requests_all_return_the_same_body(self, client: TestClient) -> None:
        expected = client.post("/actions", json={"scope": "all"}).json()
        with ThreadPoolExecutor(max_workers=self.WORKERS) as pool:
            bodies = list(
                pool.map(
                    lambda _: client.post("/actions", json={"scope": "all"}).json(),
                    range(self.REQUESTS),
                )
            )
        assert all(body == expected for body in bodies)

    def test_concurrent_requests_for_different_scopes_do_not_cross(
        self, client: TestClient
    ) -> None:
        scopes = ["all", "West", "North", "East", "South"] * 4
        expected = {s: client.post("/actions", json={"scope": s}).json() for s in set(scopes)}
        with ThreadPoolExecutor(max_workers=self.WORKERS) as pool:
            got = list(
                pool.map(lambda s: (s, client.post("/actions", json={"scope": s}).json()), scopes)
            )
        assert all(body == expected[scope] for scope, body in got)

    def test_a_cursor_per_caller_survives_the_same_load(self, prepared: tuple[Path, dict]) -> None:
        """Hammer one shared connection through per-caller cursors: results stay identical.

        The cursor is load-bearing, not decorative. Driving the same load through the raw
        connection instead corrupts the read — two threads interleave on one result set and
        the rows stop lining up with the column names, which surfaces as a ``ValueError``
        from ``rows()``'s ``strict=True`` zip or as a ``KeyError`` further along, depending
        on how the threads interleave. That failure is a race, so it is recorded here
        rather than asserted: a test that depends on threads colliding is a flaky test.
        """
        db_path, _ = prepared
        shared = duckdb.connect(str(db_path), read_only=True)
        try:
            expected = [a.model_dump() for a in run_actions(shared.cursor(), "all")]
            with ThreadPoolExecutor(max_workers=self.WORKERS) as pool:
                results = list(
                    pool.map(
                        lambda _: [a.model_dump() for a in run_actions(shared.cursor(), "all")],
                        range(self.REQUESTS),
                    )
                )
            assert all(result == expected for result in results)
            assert len(results) == self.REQUESTS
        finally:
            shared.close()
