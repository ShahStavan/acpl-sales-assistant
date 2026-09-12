"""End-to-end preparation against the real data pack.

Every assertion here is a published figure: the row counts and the national total are what
ARTEFACT.md states, so a drift in the data or the pipeline fails the suite rather than
quietly changing a number a reviewer has already read. DESIGN.md §4.4.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import sys

import duckdb
import pytest

from acpl_assistant.config import get_settings
from acpl_assistant.prepare.cli import EXPECTED_ROWS, _check_reconciliation, _check_rows, main

pytestmark = pytest.mark.integration

# The national FY26 primary-sales value total, in INR, as this system computes it.
NATIONAL_FY26_VALUE_INR = 1357631078.74

SOURCE_ROWS = {
    "fact_primary_sales": 74880,
    "fact_targets": 720,
    "stockouts": 520,
    "promotions": 40,
    "dim_sku": 120,
    "dim_geo": 12,
    "dim_distributor": 40,
    "documents": 6,
    "playbook": 8,
}


class TestSourceRows:
    def test_every_source_row_count(self, prepared: tuple[Path, dict]) -> None:
        _, report = prepared
        assert report["source_rows"] == SOURCE_ROWS

    def test_warehouse_row_counts_match_the_gates(self, prepared: tuple[Path, dict]) -> None:
        _, report = prepared
        assert report["warehouse_rows"] == EXPECTED_ROWS


class TestNationalTotal:
    def test_report_states_the_national_total(self, prepared: tuple[Path, dict]) -> None:
        _, report = prepared
        assert report["national_fy26_primary_sales_value_inr"] == NATIONAL_FY26_VALUE_INR

    def test_sql_agrees_with_the_report(self, prepared: tuple[Path, dict]) -> None:
        """Recompute in SQL: the published figure must not depend on the pandas path."""
        db_path, report = prepared
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            total = con.execute(
                "SELECT round(sum(value_inr), 2) FROM fact_primary_sales"
            ).fetchone()
        finally:
            con.close()
        assert total[0] == report["national_fy26_primary_sales_value_inr"]


class TestReconciliation:
    def test_all_seven_items_passed(self, prepared: tuple[Path, dict]) -> None:
        _, report = prepared
        assert report["all_reconciliation_passed"]
        assert [item["item"] for item in report["reconciliation"]] == [1, 2, 3, 4, 5, 6, 7]

    def test_no_sku_orphans(self, prepared: tuple[Path, dict]) -> None:
        db_path, _ = prepared
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            orphans = con.execute(
                """
                SELECT
                  (SELECT count(*) FROM fact_primary_sales f
                     LEFT JOIN dim_sku d USING (sku_code) WHERE d.sku_code IS NULL),
                  (SELECT count(*) FROM stockouts s
                     LEFT JOIN dim_sku d USING (sku_code) WHERE d.sku_code IS NULL),
                  (SELECT count(*) FROM promotions p
                     LEFT JOIN dim_sku d USING (sku_code) WHERE d.sku_code IS NULL)
                """
            ).fetchone()
        finally:
            con.close()
        assert orphans == (0, 0, 0)

    def test_every_target_cell_has_actuals(self, prepared: tuple[Path, dict]) -> None:
        """720 of 720 cells join, with no orphan on either side."""
        db_path, _ = prepared
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            joined = con.execute("SELECT count(*) FROM v_achievement").fetchone()[0]
            targets = con.execute("SELECT count(*) FROM fact_targets").fetchone()[0]
            actuals = con.execute("SELECT count(*) FROM sales_by_brand_region_month").fetchone()[0]
        finally:
            con.close()
        assert joined == targets == actuals == 720

    def test_stockout_region_derived_not_portal_text(self, prepared: tuple[Path, dict]) -> None:
        """The portal's free text is dropped; every region is one of the four real ones."""
        db_path, _ = prepared
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            regions = {
                r[0] for r in con.execute("SELECT DISTINCT region FROM stockouts").fetchall()
            }
        finally:
            con.close()
        assert regions == {"North", "South", "East", "West"}


class TestGates:
    def test_row_gate_catches_a_wrong_count(self) -> None:
        """The gate must fail on drift, not merely pass on the happy path."""
        failures = _check_rows({**EXPECTED_ROWS, "fact_primary_sales": 74879})
        assert len(failures) == 1
        assert "expected 74880 rows, warehouse holds 74879" in failures[0]

    def test_row_gate_catches_a_missing_table(self) -> None:
        counts = {k: v for k, v in EXPECTED_ROWS.items() if k != "documents"}
        failures = _check_rows(counts)
        assert len(failures) == 1
        assert "documents" in failures[0]

    def test_reconciliation_gate_catches_a_failed_item(self) -> None:
        report = {
            "reconciliation": [
                {"item": 5, "title": "Grain", "detail": "3 orphans", "passed": False}
            ]
        }
        failures = _check_reconciliation(report)
        assert len(failures) == 1
        assert "item 5" in failures[0]


class TestCommand:
    def test_main_returns_zero_and_writes_both_artefacts(self, tmp_path: Path) -> None:
        """`python prepare.py` succeeds and leaves a warehouse and a report behind."""
        settings = get_settings()
        db_path = settings.acpl_warehouse_resolved
        report_path = db_path.parent / "prep_report.json"
        assert main([]) == 0
        assert db_path.is_file()
        assert report_path.is_file()

    def test_report_is_deterministic(self, tmp_path: Path) -> None:
        """Two runs produce byte-identical reports: no timestamp, no host detail."""
        report_path = get_settings().acpl_warehouse_resolved.parent / "prep_report.json"
        assert main([]) == 0
        first = hashlib.sha256(report_path.read_bytes()).hexdigest()
        assert main([]) == 0
        second = hashlib.sha256(report_path.read_bytes()).hexdigest()
        assert first == second


class TestDataPackUntouched:
    def test_preparation_does_not_modify_the_pack(self, data_dir: Path) -> None:
        """The brief forbids editing the provided files; prove it rather than assert it."""
        before = _digest_tree(data_dir)
        assert main([]) == 0
        assert _digest_tree(data_dir) == before

    def test_git_reports_no_change_under_data(self) -> None:
        result = subprocess.run(
            ["git", "diff", "--exit-code", "--", "data/"],  # noqa: S607
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout.decode()[:400]


def _digest_tree(root: Path) -> dict[str, str]:
    """Map every file under *root* to a digest of its contents."""
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_python_prepare_runs_as_a_subprocess() -> None:
    """The documented one command works from a clean interpreter, not just in-process."""
    result = subprocess.run(
        [sys.executable, "prepare.py"],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()[-800:]
    assert b"National FY26 primary sales" in result.stdout
