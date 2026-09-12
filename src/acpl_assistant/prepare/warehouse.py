"""Write conformed tables to the single-file DuckDB warehouse and emit ``prep_report.json``.

The report records row counts, the national FY26 total and every reconciliation outcome;
ARTEFACT.md is generated from it. DESIGN.md §4.2.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb
import pandas as pd

if TYPE_CHECKING:
    from acpl_assistant.prepare.conform import ConformedPack, LogEntry, ThresholdRule

# Tables written to the warehouse, in dependency order.  ``target_by_brand_region_month``
# is deliberately absent: ``conform_pack`` sets it to the same frame as ``fact_targets``,
# so writing both would duplicate 720 rows under two names.
TABLES: tuple[str, ...] = (
    "dim_sku",
    "dim_geo",
    "dim_distributor",
    "fact_primary_sales",
    "fact_targets",
    "stockouts",
    "promotions",
    "documents",
    "playbook",
    "sales_by_brand_region_week",
    "sales_by_brand_region_month",
)

# Achievement is a view, not a table: it is a pure join of two tables already written,
# so materialising it would duplicate 720 rows and let the copies drift apart.
ACHIEVEMENT_VIEW = """
CREATE OR REPLACE VIEW v_achievement AS
SELECT
    s.month,
    s.brand,
    s.region,
    s.units,
    s.value_inr                      AS actual_value_inr,
    t.target_value_inr,
    s.value_inr / t.target_value_inr AS achievement_ratio
FROM sales_by_brand_region_month s
JOIN fact_targets t
  ON s.month = t.month AND s.brand = t.brand AND s.region = t.region
"""


def playbook_to_frame(rules: list[ThresholdRule]) -> pd.DataFrame:
    """Convert the typed playbook rules into a frame the warehouse can store."""
    return pd.DataFrame([dataclasses.asdict(r) for r in rules])


def national_fy26_value_inr(sales: pd.DataFrame) -> float:
    """Return the national FY26 primary-sales value total in INR.

    The ledger covers FY26 and nothing else, so this is the sum of every row. Rounded to
    paise because the source carries two decimal places and float summation does not.
    """
    return round(float(sales["value_inr"].sum()), 2)


def write_warehouse(pack: ConformedPack, db_path: Path) -> dict[str, int]:
    """Create ``db_path`` from scratch and write every conformed table into it.

    Returns the row count actually read back from each table, so the report states what the
    warehouse holds rather than what was handed to it.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)

    counts: dict[str, int] = {}
    con = duckdb.connect(str(db_path))
    try:
        for table in TABLES:
            value = getattr(pack, table)
            frame = playbook_to_frame(value) if table == "playbook" else value
            con.register("_staging", frame)
            con.execute(f"CREATE TABLE {table} AS SELECT * FROM _staging")  # noqa: S608
            con.unregister("_staging")
            counts[table] = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608
        con.execute(ACHIEVEMENT_VIEW)
        counts["v_achievement"] = con.execute("SELECT count(*) FROM v_achievement").fetchone()[0]
    finally:
        con.close()
    return counts


def build_report(
    counts: dict[str, int],
    log: list[LogEntry],
    national_total: float,
    source_rows: dict[str, int],
) -> dict:
    """Assemble the preparation report.

    Deterministic by design: no timestamp, no host detail, so the file is diffable and a
    changed figure shows up as a changed line rather than as noise.
    """
    return {
        "source_rows": source_rows,
        "warehouse_rows": counts,
        "national_fy26_primary_sales_value_inr": national_total,
        "reconciliation": [
            {
                "item": entry.item,
                "title": entry.title,
                "detail": entry.detail,
                "passed": entry.passed,
            }
            for entry in log
        ],
        "all_reconciliation_passed": all(entry.passed for entry in log),
    }


def write_report(report: dict, path: Path) -> None:
    r"""Write the preparation report as pretty-printed, stably ordered JSON.

    The explicit ``newline="\n"`` is not decoration. Without it Python translates newlines to CRLF
    on Windows, so identical data would produce a different file on a developer's machine
    than in CI. The report is committed, so that drift shows up as a diff nobody wrote and
    makes the repository's line-ending hook fail on every preparation run.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
