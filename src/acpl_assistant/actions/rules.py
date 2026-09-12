"""Typed representation of the eight playbook rules and their evaluation over FY26.

Thresholds are parsed once from ``action_playbook.xlsx``; ``state`` is read from the
``needs_approval`` column (R-01, R-04, R-08 → ``PENDING_APPROVAL``). DESIGN.md §5.2, §5.3.

Every rule is evaluated in SQL against the read-only warehouse, over all of FY26 and
independently of the others. A rule reports the rows it fired on and the figures that
triggered it; grouping, ranking and approval gating belong to
:mod:`acpl_assistant.actions.engine`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
import datetime as dt
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

# ---------------------------------------------------------------------------
# Thresholds — reconciliation item 7: the playbook's prose, parsed once into numbers.
# The comment above each is the condition column it was read from.
# ---------------------------------------------------------------------------

# R-01 "A brand misses target in a region (achievement < 70%) AND its SKUs have repeated
#       stock-outs there".  "Repeated" is read as two or more distinct stock-out weeks in
#       the month, the smallest count the word can honestly carry.
ACHIEVEMENT_SUPPLY_CONSTRAINED = 0.70
STOCKOUT_WEEKS_REPEATED = 2

# R-02, R-03, R-06 "A brand misses target (< 80%)".
ACHIEVEMENT_MISS = 0.80

# R-02 "while a promotion is running with weak uplift (< 10%)".
UPLIFT_WEAK = 0.10

# R-04 "A single distributor is out of stock for more than 6 weeks on a SKU".
CHRONIC_STOCKOUT_WEEKS = 6

# R-05 "A brand over-delivers against target in a region (achievement > 110%)".
ACHIEVEMENT_OVER = 1.10

# R-07 "A promotion delivered strong uplift (> 25%)".
UPLIFT_STRONG = 0.25

# R-08 "A distributor shows stock-outs across 3 or more SKUs in a month".
DISTRIBUTOR_SKUS_OUT = 3

# How many pre-promotion weeks form the uplift baseline (APPROACH.md §F).
UPLIFT_BASELINE_WEEKS = 4

# Source files, named so evidence stays traceable to the provided pack (DESIGN.md §4.6).
SALES_FILE = "fact_primary_sales.csv"
TARGETS_FILE = "fact_targets.csv"
STOCKOUTS_FILE = "stockouts.csv"
PROMOTIONS_FILE = "promotions.csv"

# Longest document excerpt carried in evidence, in characters.
EXCERPT_CHARS = 320


# ---------------------------------------------------------------------------
# Shared SQL.  Static strings with bound parameters only: nothing is interpolated
# except the module thresholds above, which are numeric literals defined in this file.
#
# Every query that feeds a finding carries an explicit ORDER BY. DuckDB aggregates in
# parallel and returns groups in whatever order the threads finish, so without one the
# same warehouse yields the same actions in a different evidence order on each call —
# which would make the published figures untestable and the responses undiffable.
# ---------------------------------------------------------------------------

# Stock-out weeks and SKUs per brand × region × month, reached through the product master.
STOCKOUTS_BY_BRAND_MONTH_SQL = """
SELECT strftime(s.week_start, '%Y-%m') AS month,
       d.brand,
       s.region,
       count(DISTINCT s.week_start) AS stockout_weeks,
       count(DISTINCT s.sku_code)   AS stockout_skus,
       sum(s.days_out_of_stock)     AS days_out_of_stock
FROM stockouts s
JOIN dim_sku d USING (sku_code)
GROUP BY 1, 2, 3
"""

# Promotions overlapping a month, matched by date range rather than month equality
# (reconciliation item 6).
PROMOTIONS_BY_BRAND_MONTH_SQL = """
WITH months AS (SELECT DISTINCT month FROM v_achievement)
SELECT m.month, d.brand, p.region, p.promo_id
FROM months m
JOIN promotions p
  ON p.start_date <= (strptime(m.month, '%Y-%m') + INTERVAL 1 MONTH - INTERVAL 1 DAY)
 AND p.end_date   >= strptime(m.month, '%Y-%m')
JOIN dim_sku d USING (sku_code)
"""

# Promotion uplift against the four preceding weeks for the same SKU × region.
#
# The roll-up to SKU × region × week must happen *before* the baseline weeks are taken:
# sales are held at SKU × territory × week and a region holds three territories, so
# selecting the last four *rows* would take barely more than one week of trading and
# inflate every uplift figure. Promotions whose window opens in the first week of the
# ledger have no prior period and drop out of the inner join — one of the forty.
PROMOTION_UPLIFT_SQL = f"""
WITH sales_by_sku_region_week AS (
    SELECT f.sku_code, g.region, f.week_start, sum(f.value_inr) AS value_inr
    FROM fact_primary_sales f
    JOIN dim_geo g USING (territory_code)
    GROUP BY 1, 2, 3
),
during AS (
    SELECT p.promo_id,
           avg(s.value_inr)  AS promo_avg_value_inr,
           min(s.week_start) AS first_week,
           count(*)          AS promo_weeks
    FROM promotions p
    JOIN sales_by_sku_region_week s
      ON s.sku_code = p.sku_code
     AND s.region   = p.region
     AND s.week_start BETWEEN p.start_date AND p.end_date
    GROUP BY 1
),
prior AS (
    SELECT d.promo_id,
           s.value_inr,
           row_number() OVER (PARTITION BY d.promo_id ORDER BY s.week_start DESC) AS rn
    FROM during d
    JOIN promotions p USING (promo_id)
    JOIN sales_by_sku_region_week s
      ON s.sku_code = p.sku_code
     AND s.region   = p.region
     AND s.week_start < d.first_week
),
baseline AS (
    SELECT promo_id,
           avg(value_inr) AS baseline_avg_value_inr,
           count(*)       AS baseline_weeks
    FROM prior
    WHERE rn <= {UPLIFT_BASELINE_WEEKS}
    GROUP BY 1
)
SELECT p.promo_id, p.sku_code, sk.brand, p.region, p.mechanic, p.discount_pct,
       p.start_date, p.end_date,
       d.promo_weeks, b.baseline_weeks,
       d.promo_avg_value_inr, b.baseline_avg_value_inr,
       d.promo_avg_value_inr / b.baseline_avg_value_inr - 1 AS uplift
FROM promotions p
JOIN during   d  USING (promo_id)
JOIN baseline b  USING (promo_id)
JOIN dim_sku  sk USING (sku_code)
ORDER BY p.promo_id
"""

# Achievement cells with their stock-out and promotion context, for the miss family.
MISS_CONTEXT_SQL = f"""
SELECT a.month, a.brand, a.region,
       a.actual_value_inr, a.target_value_inr, a.achievement_ratio,
       coalesce(s.stockout_weeks, 0)    AS stockout_weeks,
       coalesce(s.stockout_skus, 0)     AS stockout_skus,
       coalesce(s.days_out_of_stock, 0) AS days_out_of_stock,
       count(DISTINCT pm.promo_id)      AS promo_count,
       string_agg(DISTINCT pm.promo_id, ', ') AS promo_ids
FROM v_achievement a
LEFT JOIN ({STOCKOUTS_BY_BRAND_MONTH_SQL}) s USING (month, brand, region)
LEFT JOIN ({PROMOTIONS_BY_BRAND_MONTH_SQL}) pm USING (month, brand, region)
GROUP BY ALL
ORDER BY month, brand, region
"""

# One distributor × SKU out of stock for more than six weeks across FY26.
CHRONIC_STOCKOUT_SQL = f"""
SELECT s.distributor_id, dd.distributor_name, s.sku_code, sk.brand, s.region,
       count(DISTINCT s.week_start) AS weeks_out,
       min(s.week_start)            AS first_week,
       max(s.week_start)            AS last_week,
       sum(s.days_out_of_stock)     AS days_out_of_stock
FROM stockouts s
JOIN dim_distributor dd USING (distributor_id)
JOIN dim_sku sk USING (sku_code)
GROUP BY ALL
HAVING count(DISTINCT s.week_start) > {CHRONIC_STOCKOUT_WEEKS}
ORDER BY s.distributor_id, s.sku_code
"""

# One distributor short of three or more distinct SKUs within a month.
DISTRIBUTOR_MONTH_SQL = f"""
SELECT s.distributor_id, dd.distributor_name, s.region,
       strftime(s.week_start, '%Y-%m')       AS month,
       count(DISTINCT s.sku_code)            AS skus_out,
       count(DISTINCT s.week_start)          AS weeks_out,
       sum(s.days_out_of_stock)              AS days_out_of_stock,
       string_agg(DISTINCT s.sku_code, ', ') AS sku_codes
FROM stockouts s
JOIN dim_distributor dd USING (distributor_id)
GROUP BY ALL
HAVING count(DISTINCT s.sku_code) >= {DISTRIBUTOR_SKUS_OUT}
ORDER BY s.distributor_id, month
"""

# Stock-out rows behind a brand × region × month finding, for evidence.
STOCKOUT_ROWS_FOR_BRAND_SQL = """
SELECT s.distributor_id, s.sku_code, s.region, s.week_start, s.days_out_of_stock
FROM stockouts s
JOIN dim_sku d USING (sku_code)
WHERE d.brand = ? AND s.region = ? AND strftime(s.week_start, '%Y-%m') = ?
ORDER BY s.week_start, s.distributor_id, s.sku_code
"""

# Stock-out rows behind a distributor × SKU finding.
STOCKOUT_ROWS_FOR_DISTRIBUTOR_SKU_SQL = """
SELECT distributor_id, sku_code, region, week_start, days_out_of_stock
FROM stockouts
WHERE distributor_id = ? AND sku_code = ?
ORDER BY week_start
"""

# Stock-out rows behind a distributor × month finding.
STOCKOUT_ROWS_FOR_DISTRIBUTOR_MONTH_SQL = """
SELECT distributor_id, sku_code, region, week_start, days_out_of_stock
FROM stockouts
WHERE distributor_id = ? AND strftime(week_start, '%Y-%m') = ?
ORDER BY week_start, sku_code
"""

PROMOTION_ROW_SQL = """
SELECT promo_id, sku_code, region, start_date, end_date, discount_pct, mechanic
FROM promotions
WHERE promo_id = ?
"""

DOCUMENTS_SQL = """
SELECT source_file, text, brands, regions, distributors, months_resolved
FROM documents
ORDER BY source_file
"""

PLAYBOOK_SQL = "SELECT rule_id, raw_condition, action, needs_approval FROM playbook"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One rule firing on one entity in one period, with the figures that triggered it."""

    rule_id: str
    entity: tuple[str, ...]
    entity_label: str
    region: str
    period_label: str
    period_start: dt.date
    period_end: dt.date
    month_grain: bool
    figures: dict[str, Any]
    evidence: list[dict[str, Any]] = field(default_factory=list)
    value_inr: float | None = None
    """INR magnitude the rule is about: shortfall for a miss, surplus for R-05.

    ``None`` where the sources hold no rupee figure for the finding — the stock-out log
    carries days, not value, and inventing a rupee proxy for it would put an unsourced
    number into evidence.
    """
    magnitude: float = 0.0
    """Rule-family severity used to break a ranking tie: weeks out, SKUs out, uplift."""


@dataclass(frozen=True)
class RuleSpec:
    """One playbook rule: how it is evaluated, aggregated over a group, and worded."""

    rule_id: str
    evaluate: Callable[[DuckDBPyConnection], list[Finding]]
    aggregate: Callable[[Sequence[Finding]], dict[str, Any]]
    summarise: Callable[[str, str, dict[str, Any]], str]


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def _normalise(value: Any) -> Any:
    """Render a DuckDB value as something JSON can carry, dates as ``YYYY-MM-DD``."""
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return value


def rows(con: DuckDBPyConnection, sql: str, params: Sequence[Any] | None = None) -> list[dict]:
    """Run *sql* and return its rows as dicts of plain Python values."""
    cur = con.execute(sql, list(params) if params else [])
    columns = [d[0] for d in cur.description]
    return [{c: _normalise(v) for c, v in zip(columns, row, strict=True)} for row in cur.fetchall()]


def _month_bounds(month: str) -> tuple[dt.date, dt.date]:
    """Return the first and last day of a ``YYYY-MM`` month."""
    start = dt.date.fromisoformat(f"{month}-01")
    next_month = dt.date(start.year + (start.month // 12), (start.month % 12) + 1, 1)
    return start, next_month - dt.timedelta(days=1)


def _pct(ratio: float) -> int:
    """Achievement ratio as whole percentage points: what is rendered is what is grounded."""
    return round(ratio * 100)


def _inr(value: float) -> int:
    """Round a rupee figure to whole rupees, for the same reason."""
    return round(value)


def _tagged(value: str | None) -> set[str]:
    """Split one of the comma-joined tag columns written at preparation."""
    return {part.strip() for part in (value or "").split(",") if part.strip()}


# ---------------------------------------------------------------------------
# Evidence builders
# ---------------------------------------------------------------------------


def _achievement_evidence(cell: dict) -> list[dict]:
    """Two rows, because the ratio compares two files rather than appearing in either.

    The actual comes from the sales ledger and the target from the target plan; the ratio
    itself is computed here and travels in ``figures``, never in ``evidence``.
    """
    key = {"month": cell["month"], "brand": cell["brand"], "region": cell["region"]}
    return [
        {"source_file": SALES_FILE, **key, "actual_value_inr": cell["actual_value_inr"]},
        {"source_file": TARGETS_FILE, **key, "target_value_inr": cell["target_value_inr"]},
    ]


def _stockout_evidence(raw: list[dict]) -> list[dict]:
    """Tag stock-out rows with the file they came from."""
    return [{"source_file": STOCKOUTS_FILE, **row} for row in raw]


def _promotion_evidence(raw: list[dict]) -> list[dict]:
    """Tag promotion rows with the file they came from."""
    return [{"source_file": PROMOTIONS_FILE, **row} for row in raw]


def _document_evidence(doc: dict) -> dict:
    """Carry a bounded excerpt of a document, under its own file name."""
    text = " ".join(doc["text"].split())
    excerpt = text if len(text) <= EXCERPT_CHARS else text[:EXCERPT_CHARS].rstrip() + "…"
    return {
        "source_file": doc["source_file"],
        "brands": doc["brands"],
        "regions": doc["regions"],
        "months_resolved": doc["months_resolved"],
        "excerpt": excerpt,
    }


def supporting_notes(con: DuckDBPyConnection, brand: str, region: str, month: str) -> list[dict]:
    """Return documents tagged with all three of *brand*, *region* and *month*.

    The tags are the ones ``prepare/load.py`` wrote by whole-word vocabulary match; a
    month counts only where the text or the file name supplied a year, so a note naming
    "April to June" with no year cannot be pressed into service as evidence for a month
    it may not describe. This is the sole discriminator between R-03 and R-06.
    """
    return [
        doc
        for doc in rows(con, DOCUMENTS_SQL)
        if brand in _tagged(doc["brands"])
        and region in _tagged(doc["regions"])
        and month in _tagged(doc["months_resolved"])
    ]


# ---------------------------------------------------------------------------
# Aggregators — one group of findings becomes one set of figures
# ---------------------------------------------------------------------------


def _agg_achievement(findings: Sequence[Finding], value_key: str) -> dict[str, Any]:
    pcts = [f.figures["achievement_pct"] for f in findings]
    return {
        "months": len(findings),
        "achievement_pct_min": min(pcts),
        "achievement_pct_max": max(pcts),
        value_key: sum(f.figures[value_key] for f in findings),
    }


def _agg_miss(findings: Sequence[Finding]) -> dict[str, Any]:
    """R-03 and R-06: the shortfall and the achievement band."""
    return _agg_achievement(findings, "shortfall_inr")


def _agg_supply(findings: Sequence[Finding]) -> dict[str, Any]:
    """R-01: the shortfall, plus the stock-out weeks that make supply the likely cause."""
    base = _agg_achievement(findings, "shortfall_inr")
    base["stockout_weeks"] = sum(f.figures["stockout_weeks"] for f in findings)
    base["stockout_skus"] = max(f.figures["stockout_skus"] for f in findings)
    return base


def _agg_promo_miss(findings: Sequence[Finding]) -> dict[str, Any]:
    """R-02: the shortfall, plus the weakest uplift observed over the group."""
    base = _agg_achievement(findings, "shortfall_inr")
    base["uplift_pct"] = min(f.figures["uplift_pct"] for f in findings)
    return base


def _agg_over(findings: Sequence[Finding]) -> dict[str, Any]:
    """R-05: the surplus and the achievement band."""
    return _agg_achievement(findings, "surplus_inr")


def _agg_single(findings: Sequence[Finding]) -> dict[str, Any]:
    """R-04 and R-07, whose entity cannot repeat: the group is one finding."""
    return dict(findings[0].figures)


def _agg_distributor_month(findings: Sequence[Finding]) -> dict[str, Any]:
    """R-08: one call per distributor, citing every month it was short."""
    return {
        "months": len(findings),
        "skus_out_max": max(f.figures["skus_out"] for f in findings),
        "weeks_out": sum(f.figures["weeks_out"] for f in findings),
        "days_out_of_stock": sum(f.figures["days_out_of_stock"] for f in findings),
    }


# ---------------------------------------------------------------------------
# Wording.  Every numeral rendered here is a value held in ``figures``, the period or
# the entity label, which is what makes the finding text checkable against evidence.
# ---------------------------------------------------------------------------


def _band(figures: dict[str, Any]) -> str:
    """Render the achievement band, collapsing to one figure where the months agree."""
    low, high = figures["achievement_pct_min"], figures["achievement_pct_max"]
    return f"{low}%" if low == high else f"{low}–{high}%"


def _say_r01(entity: str, period: str, f: dict[str, Any]) -> str:
    return (
        f"{entity} reached {_band(f)} of target over {period}, INR {f['shortfall_inr']:,} "
        f"below plan, with its SKUs out of stock in {f['stockout_weeks']} weeks there."
    )


def _say_r02(entity: str, period: str, f: dict[str, Any]) -> str:
    return (
        f"{entity} reached {_band(f)} of target over {period}, INR {f['shortfall_inr']:,} "
        f"below plan, while a promotion ran at {f['uplift_pct']}% uplift."
    )


def _say_r03(entity: str, period: str, f: dict[str, Any]) -> str:
    return (
        f"{entity} reached {_band(f)} of target over {period}, INR {f['shortfall_inr']:,} "
        f"below plan, with no stock-out and no promotion; a field note covers the period."
    )


def _say_r04(entity: str, period: str, f: dict[str, Any]) -> str:
    return (
        f"{entity} was out of stock in {f['weeks_out']} weeks over {period}, "
        f"{f['days_out_of_stock']} days in total."
    )


def _say_r05(entity: str, period: str, f: dict[str, Any]) -> str:
    return (
        f"{entity} reached {_band(f)} of target over {period}, INR {f['surplus_inr']:,} above plan."
    )


def _say_r06(entity: str, period: str, f: dict[str, Any]) -> str:
    return (
        f"{entity} reached {_band(f)} of target over {period}, INR {f['shortfall_inr']:,} "
        f"below plan, with no stock-out, no promotion and no note explaining it."
    )


def _say_r07(entity: str, period: str, f: dict[str, Any]) -> str:
    return (
        f"{entity} delivered {f['uplift_pct']}% uplift over {period} against the "
        f"{f['baseline_weeks']} preceding weeks."
    )


def _say_r08(entity: str, period: str, f: dict[str, Any]) -> str:
    return (
        f"{entity} was short of up to {f['skus_out_max']} SKUs in a month over {period}, "
        f"{f['days_out_of_stock']} stock-out days in total."
    )


# ---------------------------------------------------------------------------
# Rule evaluators
# ---------------------------------------------------------------------------


def miss_context(con: DuckDBPyConnection) -> list[dict]:
    """Every achievement cell with the stock-out and promotion context the rules need."""
    return rows(con, MISS_CONTEXT_SQL)


def _achievement_finding(
    rule_id: str,
    cell: dict,
    *,
    extra_figures: dict[str, Any],
    extra_evidence: list[dict],
    magnitude: float,
    over: bool = False,
) -> Finding:
    """Build a finding for one brand × region × month achievement cell."""
    start, end = _month_bounds(cell["month"])
    gap = cell["target_value_inr"] - cell["actual_value_inr"]
    value_key, value = ("surplus_inr", _inr(-gap)) if over else ("shortfall_inr", _inr(gap))
    return Finding(
        rule_id=rule_id,
        entity=(cell["brand"], cell["region"]),
        entity_label=f"{cell['brand']} in the {cell['region']}",
        region=cell["region"],
        period_label=cell["month"],
        period_start=start,
        period_end=end,
        month_grain=True,
        figures={
            "month": cell["month"],
            "achievement_pct": _pct(cell["achievement_ratio"]),
            value_key: value,
            **extra_figures,
        },
        evidence=_achievement_evidence(cell) + extra_evidence,
        value_inr=float(value),
        magnitude=magnitude,
    )


def evaluate_r01(con: DuckDBPyConnection) -> list[Finding]:
    """R-01: achievement below 70% with repeated stock-outs on the brand's SKUs there."""
    found = []
    for cell in miss_context(con):
        if (
            cell["achievement_ratio"] >= ACHIEVEMENT_SUPPLY_CONSTRAINED
            or cell["stockout_weeks"] < STOCKOUT_WEEKS_REPEATED
        ):
            continue
        raw = rows(con, STOCKOUT_ROWS_FOR_BRAND_SQL, [cell["brand"], cell["region"], cell["month"]])
        found.append(
            _achievement_finding(
                "R-01",
                cell,
                extra_figures={
                    "stockout_weeks": cell["stockout_weeks"],
                    "stockout_skus": cell["stockout_skus"],
                },
                extra_evidence=_stockout_evidence(raw),
                magnitude=float(cell["stockout_weeks"]),
            )
        )
    return found


def promotion_uplift(con: DuckDBPyConnection) -> list[dict]:
    """Uplift for every measurable promotion, against its four preceding weeks."""
    return rows(con, PROMOTION_UPLIFT_SQL)


def evaluate_r02(con: DuckDBPyConnection) -> list[Finding]:
    """R-02: a miss below 80% while an overlapping promotion delivers under 10% uplift."""
    uplifts = {row["promo_id"]: row for row in promotion_uplift(con)}
    found = []
    for cell in miss_context(con):
        if cell["achievement_ratio"] >= ACHIEVEMENT_MISS or not cell["promo_count"]:
            continue
        weak = [
            uplifts[pid]
            for pid in _tagged(cell["promo_ids"])
            if pid in uplifts and uplifts[pid]["uplift"] < UPLIFT_WEAK
        ]
        if not weak:
            continue
        worst = min(weak, key=lambda p: p["uplift"])
        found.append(
            _achievement_finding(
                "R-02",
                cell,
                extra_figures={
                    "promo_id": worst["promo_id"],
                    "uplift_pct": round(worst["uplift"] * 100, 1),
                },
                extra_evidence=_promotion_evidence(
                    rows(con, PROMOTION_ROW_SQL, [worst["promo_id"]])
                ),
                magnitude=-worst["uplift"],
            )
        )
    return found


def _unexplained(con: DuckDBPyConnection, *, want_note: bool, rule_id: str) -> list[Finding]:
    """Shared body of R-03 and R-06, which differ only in whether a note exists.

    Evaluating both from one predicate is what makes them mutually exclusive by
    construction rather than by agreement between two functions.
    """
    found = []
    for cell in miss_context(con):
        if (
            cell["achievement_ratio"] >= ACHIEVEMENT_MISS
            or cell["stockout_weeks"]
            or cell["promo_count"]
        ):
            continue
        notes = supporting_notes(con, cell["brand"], cell["region"], cell["month"])
        if bool(notes) is not want_note:
            continue
        found.append(
            _achievement_finding(
                rule_id,
                cell,
                extra_figures={"supporting_notes": len(notes)},
                extra_evidence=[_document_evidence(doc) for doc in notes],
                magnitude=ACHIEVEMENT_MISS - cell["achievement_ratio"],
            )
        )
    return found


def evaluate_r03(con: DuckDBPyConnection) -> list[Finding]:
    """R-03: a miss below 80% with no stock-out, no promotion, and a note that covers it."""
    return _unexplained(con, want_note=True, rule_id="R-03")


def evaluate_r04(con: DuckDBPyConnection) -> list[Finding]:
    """R-04: one distributor out of stock on one SKU for more than six weeks in FY26."""
    found = []
    for row in rows(con, CHRONIC_STOCKOUT_SQL):
        raw = rows(
            con,
            STOCKOUT_ROWS_FOR_DISTRIBUTOR_SKU_SQL,
            [row["distributor_id"], row["sku_code"]],
        )
        found.append(
            Finding(
                rule_id="R-04",
                entity=(row["distributor_id"], row["sku_code"]),
                entity_label=(
                    f"{row['distributor_name']} ({row['distributor_id']}) on {row['sku_code']}"
                ),
                region=row["region"],
                period_label=f"{row['first_week']}..{row['last_week']}",
                period_start=dt.date.fromisoformat(row["first_week"]),
                period_end=dt.date.fromisoformat(row["last_week"]),
                month_grain=False,
                figures={
                    "weeks_out": row["weeks_out"],
                    "days_out_of_stock": row["days_out_of_stock"],
                    "brand": row["brand"],
                },
                evidence=_stockout_evidence(raw),
                value_inr=None,
                magnitude=float(row["weeks_out"]),
            )
        )
    return found


def evaluate_r05(con: DuckDBPyConnection) -> list[Finding]:
    """R-05: achievement above 110% — capture what worked and redeploy effort."""
    return [
        _achievement_finding(
            "R-05",
            cell,
            extra_figures={},
            extra_evidence=[],
            magnitude=cell["achievement_ratio"] - ACHIEVEMENT_OVER,
            over=True,
        )
        for cell in miss_context(con)
        if cell["achievement_ratio"] > ACHIEVEMENT_OVER
    ]


def evaluate_r06(con: DuckDBPyConnection) -> list[Finding]:
    """R-06: the same miss with no note — no cause is determinable, so none is attributed."""
    return _unexplained(con, want_note=False, rule_id="R-06")


def evaluate_r07(con: DuckDBPyConnection) -> list[Finding]:
    """R-07: a promotion whose uplift beat 25% against its four preceding weeks."""
    found = []
    for row in promotion_uplift(con):
        if row["uplift"] <= UPLIFT_STRONG:
            continue
        found.append(
            Finding(
                rule_id="R-07",
                entity=(row["promo_id"],),
                entity_label=(
                    f"{row['promo_id']} ({row['mechanic']} on {row['sku_code']}, "
                    f"{row['brand']} in the {row['region']})"
                ),
                region=row["region"],
                period_label=f"{row['start_date']}..{row['end_date']}",
                period_start=dt.date.fromisoformat(row["start_date"]),
                period_end=dt.date.fromisoformat(row["end_date"]),
                month_grain=False,
                figures={
                    "uplift_pct": round(row["uplift"] * 100, 1),
                    "baseline_weeks": row["baseline_weeks"],
                    "promo_weeks": row["promo_weeks"],
                    "discount_pct": row["discount_pct"],
                },
                evidence=_promotion_evidence(rows(con, PROMOTION_ROW_SQL, [row["promo_id"]])),
                value_inr=None,
                magnitude=float(row["uplift"]),
            )
        )
    return found


def evaluate_r08(con: DuckDBPyConnection) -> list[Finding]:
    """R-08: a distributor short of three or more distinct SKUs within one month."""
    found = []
    for row in rows(con, DISTRIBUTOR_MONTH_SQL):
        start, end = _month_bounds(row["month"])
        raw = rows(
            con,
            STOCKOUT_ROWS_FOR_DISTRIBUTOR_MONTH_SQL,
            [row["distributor_id"], row["month"]],
        )
        found.append(
            Finding(
                rule_id="R-08",
                entity=(row["distributor_id"],),
                entity_label=f"{row['distributor_name']} ({row['distributor_id']})",
                region=row["region"],
                period_label=row["month"],
                period_start=start,
                period_end=end,
                month_grain=True,
                figures={
                    "month": row["month"],
                    "skus_out": row["skus_out"],
                    "weeks_out": row["weeks_out"],
                    "days_out_of_stock": row["days_out_of_stock"],
                },
                evidence=_stockout_evidence(raw),
                value_inr=None,
                magnitude=float(row["skus_out"]),
            )
        )
    return found


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

RULES: dict[str, RuleSpec] = {
    "R-01": RuleSpec("R-01", evaluate_r01, _agg_supply, _say_r01),
    "R-02": RuleSpec("R-02", evaluate_r02, _agg_promo_miss, _say_r02),
    "R-03": RuleSpec("R-03", evaluate_r03, _agg_miss, _say_r03),
    "R-04": RuleSpec("R-04", evaluate_r04, _agg_single, _say_r04),
    "R-05": RuleSpec("R-05", evaluate_r05, _agg_over, _say_r05),
    "R-06": RuleSpec("R-06", evaluate_r06, _agg_miss, _say_r06),
    "R-07": RuleSpec("R-07", evaluate_r07, _agg_single, _say_r07),
    "R-08": RuleSpec("R-08", evaluate_r08, _agg_distributor_month, _say_r08),
}


def evaluate_all(con: DuckDBPyConnection) -> list[Finding]:
    """Evaluate every rule over all of FY26 and return every finding, ungrouped."""
    return [finding for spec in RULES.values() for finding in spec.evaluate(con)]
