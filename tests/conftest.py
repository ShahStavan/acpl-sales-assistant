"""Shared pytest fixtures.

The heavy fixtures are session-scoped: the pack is read once, reconciled once and written to
one throwaway warehouse, so the integration suite costs a single preparation run. No fixture
here requires a live LLM key.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from acpl_assistant.config import get_settings
from acpl_assistant.prepare.cli import REPORT_NAME
from acpl_assistant.prepare.conform import ConformedPack, LogEntry, conform_pack
from acpl_assistant.prepare.load import RawPack, load_raw_pack
from acpl_assistant.prepare.warehouse import (
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
