"""FastAPI application and process entry point.

Exposes ``POST /ask`` and ``POST /actions``; ``main()`` runs uvicorn on ``PORT``. The service
opens the DuckDB warehouse read-only and has no write path or outbound channel other than the
LLM provider. DESIGN.md §3.2.

Three things are built once and shared for the life of the process: the warehouse handle, the
vocabulary read out of it, and the provider connection. All three are expensive per request
and none of them changes between requests, so building any of them inside the handler would
show up in the latency the handler reports.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from functools import lru_cache
import json
import logging
from pathlib import Path
from typing import Annotated

import duckdb
from duckdb import DuckDBPyConnection
from fastapi import Depends, FastAPI, HTTPException, Response, status

from acpl_assistant import __version__
from acpl_assistant.actions.engine import run_actions
from acpl_assistant.ask.pipeline import AskOutcome, answer_question
from acpl_assistant.ask.resolve import Vocabulary, build_vocabulary
from acpl_assistant.config import Settings, get_settings
from acpl_assistant.llm.client import LLMClient
from acpl_assistant.obs.meter import Meter
from acpl_assistant.schemas import (
    ActionItem,
    ActionsRequest,
    AskRequest,
    AskResponse,
    HealthResponse,
    evidence_from_rows,
)

logger = logging.getLogger(__name__)

WAREHOUSE_MISSING = (
    "The warehouse is not available. Build it with `python prepare.py` before serving."
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Hold the shared provider connection open for the life of the process.

    Nothing is opened here. The warehouse and the vocabulary are built on first use so that
    a process started without a warehouse still serves ``/health`` and reports what is
    wrong, rather than failing to start and leaving the operator to read a traceback.
    """
    try:
        yield
    finally:
        # Only if one was ever built: a process that served nothing but /health and
        # /actions never touched the provider, and shutdown should not be the first
        # thing that constructs a client.
        if get_client.cache_info().currsize:
            get_client().close()
            get_client.cache_clear()


app = FastAPI(
    title="ACPL Sales Focus & Action Assistant",
    version=__version__,
    summary="Grounded answers over FY26 sales data and weekly actions from ACPL's playbook.",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Warehouse access
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def open_warehouse(path: Path) -> DuckDBPyConnection:
    """Open the warehouse read-only, once per process.

    Read-only is the service's only mode: every table was written by ``prepare.py`` and
    nothing in the request path may change one. The result is cached because opening the
    file per request would dominate the latency the endpoint reports.
    """
    return duckdb.connect(str(path), read_only=True)


@lru_cache(maxsize=1)
def get_vocabulary(path: Path) -> Vocabulary:
    """Read every name the warehouse knows, once per process.

    The vocabulary is what turns "Aqualite" into a brand the data holds and "Jaipor" into an
    ``unknown_entity`` refusal. It is derived from the dimension tables, which ``prepare.py``
    wrote and nothing at serve time can change, so reading it per request would be ten
    queries spent re-learning a fixed answer.
    """
    con = open_warehouse(path).cursor()
    try:
        return build_vocabulary(con)
    finally:
        con.close()


@lru_cache(maxsize=1)
def get_client() -> LLMClient:
    """Return the process-wide provider client, built from the cached settings.

    No argument, because :class:`~acpl_assistant.config.Settings` is not hashable and the
    settings are themselves a singleton; the cache here is over the one connection pool.
    """
    return LLMClient(get_settings())


def get_vocab(settings: Annotated[Settings, Depends(get_settings)]) -> Vocabulary:
    """Dependency wrapper over the cached vocabulary, so a test can substitute one."""
    try:
        return get_vocabulary(settings.acpl_warehouse_resolved)
    except duckdb.Error as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=WAREHOUSE_MISSING
        ) from exc


def get_connection(
    settings: Annotated[Settings, Depends(get_settings)],
) -> Iterator[DuckDBPyConnection]:
    """Yield a per-request cursor over the shared warehouse connection.

    A DuckDB connection is not safe to use from several threads at once, and FastAPI runs
    synchronous endpoints in a thread pool. A cursor per request is the supported way to
    share one open database across them.
    """
    try:
        shared = open_warehouse(settings.acpl_warehouse_resolved)
    except duckdb.Error as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=WAREHOUSE_MISSING
        ) from exc
    cursor = shared.cursor()
    try:
        yield cursor
    finally:
        cursor.close()


# ---------------------------------------------------------------------------
# Request log
# ---------------------------------------------------------------------------


def _log_request(outcome: AskOutcome, question: str) -> None:
    """Emit one structured line per ``/ask``, with the metered figures it was served under.

    The question itself is logged only at DEBUG. An operator reading INFO logs should be
    able to see cost, latency and refusal rates without also holding a transcript of what
    every user asked (DESIGN.md §6.3).
    """
    logger.info(
        "%s",
        json.dumps(
            {
                "event": "ask",
                "status": outcome.status,
                "reason": outcome.reason,
                "intent": outcome.intent,
                "evidence_rows": len(outcome.evidence),
                "models": outcome.models,
                "cost_usd": outcome.cost_usd,
                "latency_ms": outcome.latency_ms,
                "timings_ms": outcome.timings_ms,
            },
            separators=(",", ":"),
        ),
    )
    logger.debug("question: %s", question)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.post("/ask", response_model=AskResponse)
def ask(
    request: AskRequest,
    con: Annotated[DuckDBPyConnection, Depends(get_connection)],
    vocabulary: Annotated[Vocabulary, Depends(get_vocab)],
    client: Annotated[LLMClient, Depends(get_client)],
) -> AskResponse:
    """Answer one question about FY26, or say which refusal class stopped it.

    Every outcome is a 200. A question outside the catalogue, a name the data does not hold,
    a figure the verifier cannot ground and a provider that is down all return ``NO_ANSWER``
    with a reason; none of them is a server error, because none of them is this service
    malfunctioning (DESIGN.md §2.4).
    """
    meter = Meter()
    outcome = answer_question(con, client, vocabulary, request.question, meter)
    _log_request(outcome, request.question)
    return AskResponse(
        answer=outcome.answer,
        status=outcome.status,
        reason=outcome.reason,
        evidence=evidence_from_rows(outcome.evidence),
        cost_usd=outcome.cost_usd,
        latency_ms=outcome.latency_ms,
        timings_ms=outcome.timings_ms,
        intent=outcome.intent,
        models=outcome.models,
    )


@app.post("/actions", response_model=list[ActionItem])
def actions(
    request: ActionsRequest,
    con: Annotated[DuckDBPyConnection, Depends(get_connection)],
) -> list[ActionItem]:
    """Return every playbook action for *scope*, most recent and most at risk first.

    The engine is code only — no model is called and no token is spent, so the figures
    here are reproducible from the warehouse alone. A scope that resolves to no region
    returns an empty list, as does a scope with nothing to report; the two are
    deliberately indistinguishable to the caller, because neither yields an action.
    Nothing is executed in either state (DESIGN.md §5.6).
    """
    return run_actions(con, request.scope)


@app.get("/health", response_model=HealthResponse)
def health(
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
) -> HealthResponse:
    """Report whether the process can read its warehouse, and which models it may use.

    ``model`` and ``fallback_models`` are configuration, not a reachability check: the
    actions engine needs no provider at all, and ``/ask`` reports a provider failure on the
    request itself. ``degraded_models`` is observation — the models whose breaker is open
    right now — and is empty until a provider call has actually been made, because a
    process that has served only ``/health`` and ``/actions`` has never built a client and
    shutting down should not be the first thing that does.
    """
    warehouse = settings.acpl_warehouse_resolved
    degraded = list(get_client().degraded_models) if get_client.cache_info().currsize else []
    try:
        con = open_warehouse(warehouse).cursor()
        try:
            con.execute("SELECT 1")
        finally:
            con.close()
    except duckdb.Error as exc:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResponse(
            status="degraded",
            warehouse=str(warehouse),
            model=settings.LLM_MODEL,
            fallback_models=list(settings.llm_model_chain[1:]),
            degraded_models=degraded,
            detail=f"{WAREHOUSE_MISSING} ({exc.__class__.__name__})",
        )
    return HealthResponse(
        status="ok",
        warehouse=str(warehouse),
        model=settings.LLM_MODEL,
        fallback_models=list(settings.llm_model_chain[1:]),
        degraded_models=degraded,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the service with uvicorn on the configured port."""
    # Imported here rather than at module scope: importing `service` to get `app` (as the
    # tests and any ASGI host do) should not pull in the server that runs it.
    import uvicorn  # noqa: PLC0415

    settings = get_settings()
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=settings.PORT,
        log_level=settings.LOG_LEVEL.lower(),
    )


if __name__ == "__main__":
    main()
