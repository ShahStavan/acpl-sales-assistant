"""FastAPI application and process entry point.

Exposes ``POST /ask`` and ``POST /actions``; ``main()`` runs uvicorn on ``PORT``. The service
opens the DuckDB warehouse read-only and has no write path or outbound channel other than the
LLM provider. DESIGN.md §3.2.

``POST /ask`` is not registered yet: its pipeline arrives with Phase 3. A stub returning
``NO_ANSWER`` would be indistinguishable from a real refusal to the evaluation runner and
would corrupt the first accuracy measurement, so the route is absent rather than dishonest.
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Annotated

import duckdb
from duckdb import DuckDBPyConnection
from fastapi import Depends, FastAPI, HTTPException, Response, status

from acpl_assistant import __version__
from acpl_assistant.actions.engine import run_actions
from acpl_assistant.config import Settings, get_settings
from acpl_assistant.schemas import ActionItem, ActionsRequest, HealthResponse

WAREHOUSE_MISSING = (
    "The warehouse is not available. Build it with `python prepare.py` before serving."
)

app = FastAPI(
    title="ACPL Sales Focus & Action Assistant",
    version=__version__,
    summary="Grounded answers over FY26 sales data and weekly actions from ACPL's playbook.",
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
# Endpoints
# ---------------------------------------------------------------------------


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
    """Report whether the process can read its warehouse, and which model it is set to use.

    ``model`` is configuration, not a reachability check: the actions engine needs no
    provider at all, and ``/ask`` reports a provider failure on the request itself.
    """
    warehouse = settings.acpl_warehouse_resolved
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
            detail=f"{WAREHOUSE_MISSING} ({exc.__class__.__name__})",
        )
    return HealthResponse(
        status="ok",
        warehouse=str(warehouse),
        model=settings.LLM_MODEL,
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
