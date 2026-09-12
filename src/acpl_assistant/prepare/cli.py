"""``acpl-prepare`` / ``python prepare.py`` command-line entry point.

Runs the preparation pipeline end to end and writes ``warehouse.duckdb`` and
``prep_report.json``. Never modifies the provided data pack. DESIGN.md §3.2.
"""

from __future__ import annotations

from pathlib import Path
import sys

from acpl_assistant.config import get_settings
from acpl_assistant.prepare.conform import conform_pack
from acpl_assistant.prepare.load import load_raw_pack
from acpl_assistant.prepare.warehouse import (
    build_report,
    national_fy26_value_inr,
    write_report,
    write_warehouse,
)

# Row counts the prepared warehouse must hold, from DESIGN.md §4.4.  These are assertions,
# not documentation: a changed input fails the build instead of silently shifting a figure
# that ARTEFACT.md has already published.
EXPECTED_ROWS: dict[str, int] = {
    "fact_primary_sales": 74880,
    "fact_targets": 720,
    "stockouts": 520,
    "promotions": 40,
    "dim_sku": 120,
    "dim_geo": 12,
    "dim_distributor": 40,
    "documents": 6,
    "playbook": 8,
    "sales_by_brand_region_week": 3120,
    "sales_by_brand_region_month": 720,
    "v_achievement": 720,
}

REPORT_NAME = "prep_report.json"


class PreparationError(RuntimeError):
    """Raised when a gate count or a reconciliation item fails."""


def _check_rows(counts: dict[str, int]) -> list[str]:
    """Return one message per table whose row count is not what the design requires."""
    failures = []
    for table, expected in EXPECTED_ROWS.items():
        actual = counts.get(table)
        if actual != expected:
            failures.append(f"{table}: expected {expected} rows, warehouse holds {actual}")
    return failures


def _check_reconciliation(report: dict) -> list[str]:
    """Return one message per reconciliation item that did not pass."""
    return [
        f"reconciliation item {item['item']} ({item['title']}) failed: {item['detail']}"
        for item in report["reconciliation"]
        if not item["passed"]
    ]


def main(argv: list[str] | None = None) -> int:
    """Prepare the warehouse. Returns 0 on success, 1 if any gate fails."""
    argv = sys.argv[1:] if argv is None else argv
    settings = get_settings()
    data_dir = Path(argv[0]) if argv else settings.acpl_data_dir_resolved
    db_path = settings.acpl_warehouse_resolved
    report_path = db_path.parent / REPORT_NAME

    print(f"[1/5] reading data pack   {data_dir}")
    raw = load_raw_pack(data_dir)
    source_rows = {
        name: len(getattr(raw, name))
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

    print("[2/5] reconciling sources")
    pack, log = conform_pack(raw)
    for entry in log:
        mark = "ok  " if entry.passed else "FAIL"
        print(f"      {mark} {entry.item}. {entry.title}: {entry.detail}")

    print(f"[3/5] writing warehouse   {db_path}")
    counts = write_warehouse(pack, db_path)

    print(f"[4/5] writing report      {report_path}")
    national_total = national_fy26_value_inr(pack.fact_primary_sales)
    report = build_report(counts, log, national_total, source_rows)
    write_report(report, report_path)

    print("[5/5] checking gates")
    failures = _check_rows(counts) + _check_reconciliation(report)
    if failures:
        for message in failures:
            print(f"      FAIL {message}", file=sys.stderr)
        print(f"\nPreparation FAILED with {len(failures)} gate failure(s).", file=sys.stderr)
        return 1

    print(f"      ok   {len(EXPECTED_ROWS)} row gates and {len(log)} reconciliation items")
    print(f"\nPrepared. National FY26 primary sales: INR {national_total:,.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
