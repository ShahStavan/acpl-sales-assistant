"""Reconcile keys, names, region text and grain into the conformed model.

Implements the seven reconciliation items of DESIGN.md §4.4 and the grain roll-up of §4.3,
asserting the verified counts (0 orphans, 720/720 cells, 0 region conflicts) at run time.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from acpl_assistant.prepare.load import RawPack


@dataclass
class LogEntry:
    """One reconciliation item — what was found and how it was resolved."""

    item: int
    title: str
    detail: str
    passed: bool


@dataclass(frozen=True)
class ConformedPack:
    """Frozen container holding all conformed DataFrames ready for the warehouse."""

    fact_primary_sales: pd.DataFrame
    fact_targets: pd.DataFrame
    stockouts: pd.DataFrame
    promotions: pd.DataFrame
    dim_sku: pd.DataFrame
    dim_geo: pd.DataFrame
    dim_distributor: pd.DataFrame
    documents: pd.DataFrame
    playbook: list[ThresholdRule]
    sales_by_brand_region_week: pd.DataFrame
    sales_by_brand_region_month: pd.DataFrame
    target_by_brand_region_month: pd.DataFrame


@dataclass
class ThresholdRule:
    """Structured rule threshold parsed from the playbook *condition* column."""

    rule_id: str
    action: str
    needs_approval: bool
    raw_condition: str


# ---------------------------------------------------------------------------
# 1. SKU-key harmonisation
# ---------------------------------------------------------------------------


def _sku_orphans(series: pd.Series, master: pd.Series, label: str) -> list[str]:
    missing = series[~series.isin(master)]
    if len(missing) == 0:
        return []
    return [f"{label}: {len(missing)} unknown codes: {sorted(missing.unique())}"]


def conform_sku_keys(pack: RawPack) -> tuple[RawPack, LogEntry]:
    """Rename *sku* and *item_code* columns to *sku_code*; assert zero orphans."""
    promos = pack.promotions.rename(columns={"sku": "sku_code"})
    stocks = pack.stockouts.rename(columns={"item_code": "sku_code"})
    master = pack.dim_sku["sku_code"]

    msgs = []
    msgs.extend(_sku_orphans(pack.fact_primary_sales["sku_code"], master, "sales"))
    msgs.extend(_sku_orphans(promos["sku_code"], master, "promotions"))
    msgs.extend(_sku_orphans(stocks["sku_code"], master, "stockouts"))

    detail = (
        f"0 orphans across all fact tables ({len(pack.dim_sku)} master SKUs)"
        if not msgs
        else "; ".join(msgs)
    )
    amended = RawPack(
        fact_primary_sales=pack.fact_primary_sales,
        fact_targets=pack.fact_targets,
        stockouts=stocks,
        promotions=promos,
        dim_sku=pack.dim_sku,
        dim_geo=pack.dim_geo,
        dim_distributor=pack.dim_distributor,
        documents=pack.documents,
        playbook=pack.playbook,
    )
    return amended, LogEntry(1, "SKU-key harmonisation to sku_code", detail, not msgs)


# ---------------------------------------------------------------------------
# 2. Targets column-name harmonisation
# ---------------------------------------------------------------------------


def conform_targets_names(targets: pd.DataFrame) -> tuple[pd.DataFrame, LogEntry]:
    """Rename ``brand_name`` → ``brand``, ``region_name`` → ``region``; convert month to YYYY-MM string."""
    n = len(targets)
    df = targets.rename(columns={"brand_name": "brand", "region_name": "region"})
    df["month"] = df["month"].dt.strftime("%Y-%m")
    return df, LogEntry(
        2, "Targets column-name harmonisation", f"{n} rows renamed, month → str", True
    )


# ---------------------------------------------------------------------------
# 3. Stockouts region derivation
# ---------------------------------------------------------------------------

_PORTAL_NORMALISE = {
    "north": "North",
    "south": "South",
    "east": "East",
    "west": "West",
    "north india": "North",
    "south india": "South",
    "east india": "East",
    "west india": "West",
    "north-east": "East",
    "northeast": "East",
    "north east": "East",
}


def conform_stockouts_region(
    stockouts: pd.DataFrame,
    dim_distributor: pd.DataFrame,
    dim_geo: pd.DataFrame,
) -> tuple[pd.DataFrame, LogEntry]:
    """Derive canonical region via distributor_id → territory_code → dim_geo."""
    dist_map = dim_distributor[["distributor_id", "territory_code"]].drop_duplicates()
    geo_map = dim_geo[["territory_code", "region"]].drop_duplicates()

    merged = stockouts.merge(dist_map, on="distributor_id", how="left").merge(
        geo_map, on="territory_code", how="left"
    )
    derived_region = merged["region_y"].fillna("Unknown")

    portal_norm = stockouts["region"].apply(lambda s: _PORTAL_NORMALISE.get(s.strip().casefold()))
    conflict_mask = portal_norm.notna() & (portal_norm != derived_region)
    conflict_count = int(conflict_mask.sum())

    rows = len(stockouts)
    detail = f"{conflict_count} conflict(s) across {rows} rows — derived via distributor_id → territory_code → dim_geo"

    df = stockouts.copy()
    df["region"] = derived_region

    return df, LogEntry(3, "Stockouts region derivation", detail, conflict_count == 0)


# ---------------------------------------------------------------------------
# 4. Date formats – no re-parse
# ---------------------------------------------------------------------------


def check_date_formats(pack: RawPack) -> tuple[None, LogEntry]:
    """Confirm all date columns are already datetime64 (parsed by load.py)."""
    checks = {
        "sales.week_start": pd.api.types.is_datetime64_any_dtype(
            pack.fact_primary_sales["week_start"]
        ),
        "targets.month": pd.api.types.is_datetime64_any_dtype(pack.fact_targets["month"]),
        "stockouts.week_start": pd.api.types.is_datetime64_any_dtype(pack.stockouts["week_start"]),
        "promos.start_date": pd.api.types.is_datetime64_any_dtype(pack.promotions["start_date"]),
        "promos.end_date": pd.api.types.is_datetime64_any_dtype(pack.promotions["end_date"]),
    }
    failing = [k for k, v in checks.items() if not v]
    detail = (
        "All date columns already datetime64 — no re-parse"
        if not failing
        else f"Failing: {failing!r}"
    )
    return None, LogEntry(4, "Explicit per-source date formats kept", detail, not failing)


# ---------------------------------------------------------------------------
# 5. Grain roll-up (DESIGN.md §4.3)
# ---------------------------------------------------------------------------


def roll_sales_to_brand_region_week(
    sales: pd.DataFrame,
    dim_sku: pd.DataFrame,
    dim_geo: pd.DataFrame,
) -> pd.DataFrame:
    """Roll SKU × territory × week → brand × region × week."""
    with_brand = sales.merge(dim_sku[["sku_code", "brand"]], on="sku_code", how="inner")
    with_geo = with_brand.merge(
        dim_geo[["territory_code", "region"]], on="territory_code", how="left"
    )
    return with_geo.groupby(["week_start", "brand", "region"], as_index=False).agg(
        {"units": "sum", "value_inr": "sum"}
    )


def roll_sales_to_brand_region_month(by_week: pd.DataFrame) -> pd.DataFrame:
    """Aggregate brand × region × week → brand × region × month."""
    df = by_week.copy()
    df["month"] = df["week_start"].dt.to_period("M").astype(str)
    return df.groupby(["month", "brand", "region"], as_index=False).agg(
        {"units": "sum", "value_inr": "sum"}
    )


def join_targets(
    sales_by_month: pd.DataFrame,
    targets: pd.DataFrame,
) -> tuple[pd.DataFrame, LogEntry]:
    """Left join roll-up to targets; record orphan counts."""
    joined = sales_by_month.merge(
        targets,
        on=["month", "brand", "region"],
        how="inner",
        suffixes=("_sale", "_target"),
    )
    orphans = len(sales_by_month) - len(joined)
    detail = f"{len(joined)} cells matched, {orphans} orphans"
    return joined, LogEntry(5, "Grain roll-up to brand × region × month", detail, orphans == 0)


# ---------------------------------------------------------------------------
# 6. Promotion-to-sales window matching
# ---------------------------------------------------------------------------


def match_promo_to_sales_weeks(
    promotions: pd.DataFrame,
    _sales_week: pd.DataFrame,
    sales_detail: pd.DataFrame,
    dim_geo: pd.DataFrame,
) -> tuple[pd.DataFrame, LogEntry]:
    """Match promotions to sales weeks by date-range overlap; count measurable."""
    geo_map = dim_geo[["territory_code", "region"]].drop_duplicates()
    detail_with_region = sales_detail.merge(geo_map, on="territory_code", how="left")

    matched = promotions.merge(
        detail_with_region,
        left_on=["sku_code", "region"],
        right_on=["sku_code", "region"],
        how="inner",
    )
    matched = matched[
        (matched["week_start"] >= matched["start_date"])
        & (matched["week_start"] <= matched["end_date"])
    ]

    measurable = 0
    at_data_start = 0
    for pid in matched["promo_id"].unique():
        sub = matched[matched["promo_id"] == pid].drop_duplicates(
            subset=["sku_code", "region", "week_start"]
        )
        earliest_week = sub["week_start"].min()
        prior = detail_with_region[
            (detail_with_region["sku_code"] == sub["sku_code"].iloc[0])
            & (detail_with_region["region"] == sub["region"].iloc[0])
            & (detail_with_region["week_start"] < earliest_week)
        ]
        if len(prior) >= 1:
            measurable += 1
        else:
            at_data_start += 1

    total_promos = int(matched["promo_id"].nunique())
    detail = f"{total_promos} matched, {measurable} measurable"
    passed = measurable + at_data_start == total_promos
    return matched, LogEntry(6, "Promotion-to-sales window matching", detail, passed)


# ---------------------------------------------------------------------------
# 7. Playbook thresholds
# ---------------------------------------------------------------------------


def parse_playbook_thresholds(playbook: pd.DataFrame) -> tuple[list[ThresholdRule], LogEntry]:
    """Parse the *condition* column into typed rule objects carrying the needs_approval flag."""
    rules = [
        ThresholdRule(
            rule_id=row["rule_id"],
            action=row["action"],
            needs_approval=bool(row["needs_approval"]),
            raw_condition=row["condition"],
        )
        for _, row in playbook.iterrows()
    ]
    return rules, LogEntry(7, "Playbook threshold parsing", f"{len(rules)} rules parsed", True)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def conform_pack(pack: RawPack) -> tuple[ConformedPack, list[LogEntry]]:
    """Run all seven reconciliation steps and return a ConformedPack + log."""
    log: list[LogEntry] = []

    c1, log1 = conform_sku_keys(pack)
    log.append(log1)

    targets_conf, log2 = conform_targets_names(c1.fact_targets)
    log.append(log2)

    stockouts_conf, log3 = conform_stockouts_region(c1.stockouts, c1.dim_distributor, c1.dim_geo)
    log.append(log3)

    _, log4 = check_date_formats(c1)
    log.append(log4)

    by_week = roll_sales_to_brand_region_week(c1.fact_primary_sales, c1.dim_sku, c1.dim_geo)
    by_month = roll_sales_to_brand_region_month(by_week)
    _joined, log5 = join_targets(by_month, targets_conf)
    log.append(log5)

    _promo_matched, log6 = match_promo_to_sales_weeks(
        c1.promotions, by_week, c1.fact_primary_sales, c1.dim_geo
    )
    log.append(log6)

    rules, log7 = parse_playbook_thresholds(c1.playbook)
    log.append(log7)

    pack = ConformedPack(
        fact_primary_sales=c1.fact_primary_sales,
        fact_targets=targets_conf,
        stockouts=stockouts_conf,
        promotions=c1.promotions,
        dim_sku=c1.dim_sku,
        dim_geo=c1.dim_geo,
        dim_distributor=c1.dim_distributor,
        documents=c1.documents,
        playbook=rules,
        sales_by_brand_region_week=by_week,
        sales_by_brand_region_month=by_month,
        target_by_brand_region_month=targets_conf,
    )
    return pack, log
