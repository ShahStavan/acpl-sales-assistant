"""Shared pytest fixtures.

The heavy fixtures are session-scoped: the pack is read once, reconciled once and written to
one throwaway warehouse, so the integration suite costs a single preparation run. No fixture
here requires a live LLM key.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import json
from pathlib import Path

import duckdb
import pytest

from acpl_assistant.config import get_settings
from acpl_assistant.prepare.cli import REPORT_NAME
from acpl_assistant.prepare.conform import ConformedPack, LogEntry, conform_pack
from acpl_assistant.prepare.load import RawPack, load_raw_pack
from acpl_assistant.prepare.warehouse import (
    ACHIEVEMENT_VIEW,
    build_report,
    national_fy26_value_inr,
    write_report,
    write_warehouse,
)


@pytest.fixture(scope="session")
def data_dir() -> Path:
    """Absolute path to the provided data pack, which tests must only ever read."""
    return get_settings().acpl_data_dir_resolved


@pytest.fixture(scope="session")
def raw_pack(data_dir: Path) -> RawPack:
    """The data pack as loaded, before any reconciliation."""
    return load_raw_pack(data_dir)


@pytest.fixture(scope="session")
def conformed(raw_pack: RawPack) -> tuple[ConformedPack, list[LogEntry]]:
    """The conformed pack and its reconciliation log."""
    return conform_pack(raw_pack)


@pytest.fixture(scope="session")
def prepared(
    conformed: tuple[ConformedPack, list[LogEntry]],
    raw_pack: RawPack,
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, dict]:
    """Build a throwaway warehouse and return its path plus the preparation report.

    Written under pytest's temp directory rather than the repo root, so running the suite
    never overwrites the warehouse a developer or the service is using.
    """
    pack, log = conformed
    db_path = tmp_path_factory.mktemp("warehouse") / "warehouse.duckdb"
    counts = write_warehouse(pack, db_path)
    source_rows = {
        name: len(getattr(raw_pack, name))
        for name in (
            "fact_primary_sales",
            "fact_targets",
            "stockouts",
            "promotions",
            "dim_sku",
            "dim_geo",
            "dim_distributor",
            "documents",
            "playbook",
        )
    }
    report = build_report(
        counts, log, national_fy26_value_inr(pack.fact_primary_sales), source_rows
    )
    report_path = db_path.parent / REPORT_NAME
    write_report(report, report_path)
    return db_path, json.loads(report_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Synthetic warehouses for the rules engine
# ---------------------------------------------------------------------------

# Fixed masters, small enough to reason about: two regions, two distributors, three SKUs
# across two brands.  Rule tests vary the facts, never the masters.
SYNTHETIC_GEO = [
    ("T-N", "North Territory", "North", "Delhi"),
    ("T-W", "West Territory", "West", "Maharashtra"),
]
SYNTHETIC_SKU = [
    ("K-1", "Alpha 1L", "BrandA", "Beverages", "1L", 100),
    ("K-2", "Alpha 2L", "BrandA", "Beverages", "2L", 180),
    ("K-3", "Beta 500g", "BrandB", "Snacks", "500g", 60),
]
SYNTHETIC_DISTRIBUTOR = [
    ("D1", "North Traders", "T-N", "Delhi"),
    ("D2", "West Traders", "T-W", "Mumbai"),
]

# The eight rules with their real approval flags, so a synthetic warehouse gates the way
# the provided playbook does unless a test deliberately says otherwise.
SYNTHETIC_PLAYBOOK = [
    ("R-01", "Expedite replenishment", True, "achievement < 70% AND repeated stock-outs"),
    ("R-02", "Review promo effectiveness", False, "misses target (< 80%) with weak uplift"),
    ("R-03", "Commission a market visit", False, "misses target with no stock-out, no promo"),
    ("R-04", "Raise a replenishment order", True, "out of stock more than 6 weeks on a SKU"),
    ("R-05", "Capture what worked", False, "achievement > 110%"),
    ("R-06", "Flag for manual review", False, "no stock-out, no promotion, no note"),
    ("R-07", "Extend or replicate the mechanic", False, "uplift > 25%"),
    ("R-08", "Schedule a stock-review call", True, "3 or more SKUs out in a month"),
]

_SCHEMA = """
CREATE TABLE dim_geo (
    territory_code VARCHAR, territory_name VARCHAR, region VARCHAR, state VARCHAR);
CREATE TABLE dim_sku (
    sku_code VARCHAR, sku_name VARCHAR, brand VARCHAR, category VARCHAR,
    pack_size VARCHAR, mrp_inr BIGINT);
CREATE TABLE dim_distributor (
    distributor_id VARCHAR, distributor_name VARCHAR, territory_code VARCHAR, city VARCHAR);
CREATE TABLE fact_primary_sales (
    sku_code VARCHAR, territory_code VARCHAR, week_start TIMESTAMP,
    units BIGINT, value_inr DOUBLE);
CREATE TABLE fact_targets (
    month VARCHAR, brand VARCHAR, region VARCHAR, target_value_inr BIGINT);
CREATE TABLE stockouts (
    distributor_id VARCHAR, sku_code VARCHAR, week_start TIMESTAMP,
    region VARCHAR, days_out_of_stock BIGINT);
CREATE TABLE promotions (
    promo_id VARCHAR, sku_code VARCHAR, region VARCHAR, start_date TIMESTAMP,
    end_date TIMESTAMP, discount_pct BIGINT, mechanic VARCHAR);
CREATE TABLE documents (
    source_file VARCHAR, text VARCHAR, brands VARCHAR, regions VARCHAR,
    distributors VARCHAR, months VARCHAR, months_resolved VARCHAR);
CREATE TABLE playbook (
    rule_id VARCHAR, action VARCHAR, needs_approval BOOLEAN, raw_condition VARCHAR);
CREATE TABLE sales_by_brand_region_month (
    month VARCHAR, brand VARCHAR, region VARCHAR, units BIGINT, value_inr DOUBLE);
"""


def build_synthetic_warehouse(
    con: duckdb.DuckDBPyConnection,
    *,
    achievement: Sequence[tuple] = (),
    stockouts: Sequence[tuple] = (),
    promotions: Sequence[tuple] = (),
    sales: Sequence[tuple] = (),
    documents: Sequence[tuple] = (),
    playbook: Sequence[tuple] = SYNTHETIC_PLAYBOOK,
) -> duckdb.DuckDBPyConnection:
    """Create a warehouse with the production schema and the caller's facts.

    ``achievement`` rows are ``(month, brand, region, actual_value_inr, target_value_inr)``
    and populate both sides of the ``v_achievement`` join, which is how a test puts a cell
    at an exact ratio without arranging 74,880 sales rows to produce it.
    """
    con.execute(_SCHEMA)
    con.executemany("INSERT INTO dim_geo VALUES (?, ?, ?, ?)", SYNTHETIC_GEO)
    con.executemany("INSERT INTO dim_sku VALUES (?, ?, ?, ?, ?, ?)", SYNTHETIC_SKU)
    con.executemany("INSERT INTO dim_distributor VALUES (?, ?, ?, ?)", SYNTHETIC_DISTRIBUTOR)
    con.executemany("INSERT INTO playbook VALUES (?, ?, ?, ?)", list(playbook))

    for month, brand, region, actual, target in achievement:
        con.execute(
            "INSERT INTO sales_by_brand_region_month VALUES (?, ?, ?, ?, ?)",
            [month, brand, region, 0, actual],
        )
        con.execute("INSERT INTO fact_targets VALUES (?, ?, ?, ?)", [month, brand, region, target])

    if stockouts:
        con.executemany("INSERT INTO stockouts VALUES (?, ?, ?, ?, ?)", list(stockouts))
    if promotions:
        con.executemany("INSERT INTO promotions VALUES (?, ?, ?, ?, ?, ?, ?)", list(promotions))
    if sales:
        con.executemany("INSERT INTO fact_primary_sales VALUES (?, ?, ?, ?, ?)", list(sales))
    if documents:
        con.executemany("INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?)", list(documents))

    con.execute(ACHIEVEMENT_VIEW)
    return con


@pytest.fixture()
def synthetic() -> Callable[..., duckdb.DuckDBPyConnection]:
    """Return a factory building an in-memory warehouse from the facts a test supplies."""
    connections: list[duckdb.DuckDBPyConnection] = []

    def factory(**facts: object) -> duckdb.DuckDBPyConnection:
        con = duckdb.connect(":memory:")
        connections.append(con)
        return build_synthetic_warehouse(con, **facts)  # type: ignore[arg-type]

    yield factory
    for con in connections:
        con.close()
