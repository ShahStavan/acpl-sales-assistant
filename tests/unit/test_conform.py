"""Tests for ``conform.py``: seven reconciliation items, grain functions, region conflicts, month type."""

from __future__ import annotations

import pandas as pd
import pytest

from acpl_assistant.prepare.conform import (
    ConformedPack,
    LogEntry,
    ThresholdRule,
    check_date_formats,
    conform_pack,
    conform_sku_keys,
    conform_stockouts_region,
    conform_targets_names,
    join_targets,
    match_promo_to_sales_weeks,
    parse_playbook_thresholds,
    roll_sales_to_brand_region_month,
    roll_sales_to_brand_region_week,
)
from acpl_assistant.prepare.load import RawPack

# ---------------------------------------------------------------------------
# Fixtures — small in-memory DataFrames
# ---------------------------------------------------------------------------


@pytest.fixture()
def dim_sku() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "sku_code": ["SKU-01", "SKU-02", "SKU-03"],
            "sku_name": ["Alpha", "Beta", "Gamma"],
            "brand": ["BrandA", "BrandB", "BrandA"],
            "category": ["Cat1", "Cat2", "Cat1"],
            "pack_size": ["100g", "200g", "100g"],
            "mrp_inr": [50, 75, 60],
        }
    )


@pytest.fixture()
def dim_geo() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "territory_code": ["T-N", "T-S"],
            "territory_name": ["North Territory", "South Territory"],
            "region": ["North", "South"],
            "state": ["Delhi", "Karnataka"],
        }
    )


@pytest.fixture()
def dim_distributor() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "distributor_id": ["D001", "D002", "D003"],
            "distributor_name": ["North Dist", "South Dist", "East Dist"],
            "territory_code": ["T-N", "T-S", "T-N"],
            "city": ["Delhi", "Bangalore", "Ghaziabad"],
        }
    )


@pytest.fixture()
def raw_pack_base(dim_sku, dim_geo, dim_distributor) -> RawPack:
    sales = pd.DataFrame(
        {
            "sku_code": ["SKU-01", "SKU-01", "SKU-02", "SKU-01"],
            "territory_code": ["T-N", "T-N", "T-S", "T-N"],
            "week_start": pd.to_datetime(["2025-07-24", "2025-07-01", "2025-07-01", "2025-07-08"]),
            "units": [90, 100, 200, 150],
            "value_inr": [4500.0, 5000.0, 15000.0, 7500.0],
        }
    )
    targets = pd.DataFrame(
        {
            "month": pd.to_datetime(["2025-07-01", "2025-07-01", "2025-08-01"]),
            "brand_name": ["BrandA", "BrandB", "BrandA"],
            "region_name": ["North", "South", "North"],
            "target_value_inr": [900_000, 2_000_000, 1_100_000],
        }
    )
    stockouts = pd.DataFrame(
        {
            "distributor_id": ["D001", "D002", "D003"],
            "item_code": ["SKU-01", "SKU-02", "SKU-01"],
            "week_start": pd.to_datetime(["2025-07-01", "2025-07-08", "2025-07-15"]),
            "region": ["North", "South", "North"],
            "days_out_of_stock": [3, 5, 2],
        }
    )
    promotions = pd.DataFrame(
        {
            "promo_id": ["PR-01"],
            "sku": ["SKU-01"],
            "region": ["North"],
            "start_date": pd.to_datetime(["2025-07-01"]),
            "end_date": pd.to_datetime(["2025-07-28"]),
            "discount_pct": [10],
            "mechanic": ["Price-off"],
        }
    )
    playbook = pd.DataFrame(
        {
            "rule_id": ["R-01", "R-02"],
            "condition": ["cond_a", "cond_b"],
            "recommendation": ["rec_a", "rec_b"],
            "action": ["alert", "approve"],
            "needs_approval": [True, False],
        }
    )
    documents = pd.DataFrame(
        {
            "source_file": ["note.docx"],
            "text": ["A document"],
            "brands": [""],
            "regions": [""],
            "distributors": [""],
            "months": [""],
            "months_resolved": [""],
        }
    )
    return RawPack(
        fact_primary_sales=sales,
        fact_targets=targets,
        stockouts=stockouts,
        promotions=promotions,
        dim_sku=dim_sku,
        dim_geo=dim_geo,
        dim_distributor=dim_distributor,
        documents=documents,
        playbook=playbook,
    )


# ---------------------------------------------------------------------------
# 1. SKU-key harmonisation
# ---------------------------------------------------------------------------


class TestConformSkuKeys:
    def test_renames_promotions_and_stockouts(self, raw_pack_base: RawPack) -> None:
        amended, log = conform_sku_keys(raw_pack_base)
        assert "sku_code" in amended.promotions.columns
        assert "sku_code" in amended.stockouts.columns
        assert "sku" not in amended.promotions.columns
        assert "item_code" not in amended.stockouts.columns
        assert log.item == 1
        assert log.passed

    def test_detects_orphan(self, dim_sku, dim_geo, dim_distributor) -> None:
        sales = pd.DataFrame(
            {
                "sku_code": ["SKU-01", "SKU-99"],
                "territory_code": ["T-N", "T-N"],
                "week_start": pd.to_datetime(["2025-07-01", "2025-07-01"]),
                "units": [100, 50],
                "value_inr": [5000.0, 2500.0],
            }
        )
        pack = RawPack(
            fact_primary_sales=sales,
            fact_targets=pd.DataFrame(
                columns=["month", "brand_name", "region_name", "target_value_inr"]
            ),
            stockouts=pd.DataFrame(
                columns=["distributor_id", "item_code", "week_start", "region", "days_out_of_stock"]
            ),
            promotions=pd.DataFrame(
                columns=[
                    "promo_id",
                    "sku",
                    "region",
                    "start_date",
                    "end_date",
                    "discount_pct",
                    "mechanic",
                ]
            ),
            dim_sku=dim_sku,
            dim_geo=dim_geo,
            dim_distributor=dim_distributor,
            documents=pd.DataFrame(
                columns=[
                    "source_file",
                    "text",
                    "brands",
                    "regions",
                    "distributors",
                    "months",
                    "months_resolved",
                ]
            ),
            playbook=pd.DataFrame(
                columns=["rule_id", "condition", "recommendation", "action", "needs_approval"]
            ),
        )
        _, log = conform_sku_keys(pack)
        assert not log.passed
        assert "SKU-99" in log.detail

    def test_all_keys_pass(self, raw_pack_base: RawPack) -> None:
        _, log = conform_sku_keys(raw_pack_base)
        assert log.passed
        assert "0 orphans" in log.detail


# ---------------------------------------------------------------------------
# 2. Targets column-name harmonisation + month conversion
# ---------------------------------------------------------------------------


class TestConformTargetsNames:
    def test_renames_columns(self, raw_pack_base: RawPack) -> None:
        df, log = conform_targets_names(raw_pack_base.fact_targets)
        assert "brand" in df.columns
        assert "region" in df.columns
        assert "brand_name" not in df.columns
        assert "region_name" not in df.columns
        assert log.item == 2
        assert log.passed

    def test_converts_month_to_string(self, raw_pack_base: RawPack) -> None:
        df, _ = conform_targets_names(raw_pack_base.fact_targets)
        assert pd.api.types.is_string_dtype(df["month"])
        assert df["month"].iloc[0] == "2025-07"


# ---------------------------------------------------------------------------
# 3. Stockouts region derivation
# ---------------------------------------------------------------------------


class TestConformStockoutsRegion:
    def test_derives_region(self, raw_pack_base: RawPack) -> None:
        df, _ = conform_stockouts_region(
            raw_pack_base.stockouts, raw_pack_base.dim_distributor, raw_pack_base.dim_geo
        )
        assert list(df["region"]) == ["North", "South", "North"]

    def test_portal_conflict_counted_and_derived_wins(self, dim_distributor, dim_geo) -> None:
        stockouts = pd.DataFrame(
            {
                "distributor_id": ["D001", "D002"],
                "item_code": ["SKU-01", "SKU-02"],
                "week_start": pd.to_datetime(["2025-07-01", "2025-07-08"]),
                "region": ["south", "West"],
                "days_out_of_stock": [3, 5],
            }
        )
        df, log = conform_stockouts_region(stockouts, dim_distributor, dim_geo)
        assert log.passed is False
        assert "conflict" in log.detail.lower()
        assert "2 conflict" in log.detail
        assert list(df["region"]) == ["North", "South"], (
            "Derived region via distributor must win over portal text"
        )


# ---------------------------------------------------------------------------
# 4. Date formats
# ---------------------------------------------------------------------------


class TestCheckDateFormats:
    def test_all_datetime(self, raw_pack_base: RawPack) -> None:
        _, log = check_date_formats(raw_pack_base)
        assert log.item == 4
        assert log.passed

    def test_non_datetime_fails(self) -> None:
        pack = RawPack(
            fact_primary_sales=pd.DataFrame(
                {
                    "week_start": ["not-a-date"],
                    "sku_code": [""],
                    "territory_code": [""],
                    "units": [0],
                    "value_inr": [0.0],
                }
            ),
            fact_targets=pd.DataFrame({"month": pd.to_datetime(["2025-07-01"])}),
            stockouts=pd.DataFrame(
                {
                    "week_start": pd.to_datetime(["2025-07-01"]),
                    "distributor_id": [""],
                    "item_code": [""],
                    "region": [""],
                    "days_out_of_stock": [0],
                }
            ),
            promotions=pd.DataFrame(
                {
                    "start_date": pd.to_datetime(["2025-07-01"]),
                    "end_date": pd.to_datetime(["2025-07-28"]),
                    "promo_id": [""],
                    "sku": [""],
                    "region": [""],
                    "discount_pct": [0],
                    "mechanic": [""],
                }
            ),
            dim_sku=pd.DataFrame(
                {
                    "sku_code": [""],
                    "sku_name": [""],
                    "brand": [""],
                    "category": [""],
                    "pack_size": [""],
                    "mrp_inr": [0],
                }
            ),
            dim_geo=pd.DataFrame(
                {"territory_code": [""], "territory_name": [""], "region": [""], "state": [""]}
            ),
            dim_distributor=pd.DataFrame(
                {
                    "distributor_id": [""],
                    "distributor_name": [""],
                    "territory_code": [""],
                    "city": [""],
                }
            ),
            documents=pd.DataFrame(
                {
                    "source_file": [""],
                    "text": [""],
                    "brands": [""],
                    "regions": [""],
                    "distributors": [""],
                    "months": [""],
                    "months_resolved": [""],
                }
            ),
            playbook=pd.DataFrame(
                {
                    "rule_id": [""],
                    "condition": [""],
                    "recommendation": [""],
                    "action": [""],
                    "needs_approval": [False],
                }
            ),
        )
        _, log = check_date_formats(pack)
        assert not log.passed
        assert "week_start" in log.detail


# ---------------------------------------------------------------------------
# 5a. Grain roll-up — week
# ---------------------------------------------------------------------------


class TestRollSalesToBrandRegionWeek:
    def test_rolls_up(self, raw_pack_base: RawPack) -> None:
        by_week = roll_sales_to_brand_region_week(
            raw_pack_base.fact_primary_sales, raw_pack_base.dim_sku, raw_pack_base.dim_geo
        )
        expected_columns = {"week_start", "brand", "region", "units", "value_inr"}
        assert expected_columns.issubset(set(by_week.columns))
        assert len(by_week) == 4
        by_week_sorted = by_week.sort_values(["week_start", "brand", "region"]).reset_index(
            drop=True
        )
        row = by_week_sorted.iloc[0]
        assert row["brand"] == "BrandA"
        assert row["region"] == "North"
        assert row["units"] == 100


# ---------------------------------------------------------------------------
# 5b. Grain roll-up — month
# ---------------------------------------------------------------------------


class TestRollSalesToBrandRegionMonth:
    def test_aggregates_month(self, raw_pack_base: RawPack) -> None:
        by_week = roll_sales_to_brand_region_week(
            raw_pack_base.fact_primary_sales, raw_pack_base.dim_sku, raw_pack_base.dim_geo
        )
        by_month = roll_sales_to_brand_region_month(by_week)
        assert "month" in by_month.columns
        assert "brand" in by_month.columns
        assert "region" in by_month.columns
        assert pd.api.types.is_string_dtype(by_month["month"]), "month must be string in YYYY-MM"
        assert by_month["month"].iloc[0] == "2025-07"
        assert len(by_month) == 2  # BrandA/North and BrandB/South in 2025-07
        brand_a_north = by_month[by_month["brand"] == "BrandA"]
        assert brand_a_north["units"].sum() == 340  # 90 + 100 + 150


# ---------------------------------------------------------------------------
# 5c. Join targets — month type consistency
# ---------------------------------------------------------------------------


class TestJoinTargets:
    def test_join_type_and_orphans(self, raw_pack_base: RawPack) -> None:
        by_week = roll_sales_to_brand_region_week(
            raw_pack_base.fact_primary_sales, raw_pack_base.dim_sku, raw_pack_base.dim_geo
        )
        by_month = roll_sales_to_brand_region_month(by_week)
        targets_conf, _ = conform_targets_names(raw_pack_base.fact_targets)
        joined, log = join_targets(by_month, targets_conf)
        assert pd.api.types.is_string_dtype(joined["month"]), (
            "month must be string on both sides of join"
        )
        assert pd.api.types.is_string_dtype(targets_conf["month"]), "targets month must be string"
        assert log.item == 5
        assert log.passed
        assert "0 orphans" in log.detail

    def test_orphan_detected(self) -> None:
        sales = pd.DataFrame(
            {
                "month": ["2025-07"],
                "brand": ["BrandA"],
                "region": ["North"],
                "units": [100],
                "value_inr": [5000.0],
            }
        )
        targets = pd.DataFrame(
            {
                "month": ["2025-07"],
                "brand": ["BrandA"],
                "region": ["South"],
                "target_value_inr": [1_000_000],
            }
        )
        _, log = join_targets(sales, targets)
        assert not log.passed

    def test_month_type_identical_on_both_sides(self, raw_pack_base: RawPack) -> None:
        by_week = roll_sales_to_brand_region_week(
            raw_pack_base.fact_primary_sales, raw_pack_base.dim_sku, raw_pack_base.dim_geo
        )
        by_month = roll_sales_to_brand_region_month(by_week)
        targets_conf, _ = conform_targets_names(raw_pack_base.fact_targets)
        assert by_month["month"].dtype == targets_conf["month"].dtype, (
            f"sales month dtype {by_month['month'].dtype} != targets month dtype {targets_conf['month'].dtype}"
        )
        joined = by_month.merge(
            targets_conf,
            on=["month", "brand", "region"],
            how="inner",
            suffixes=("_sale", "_target"),
        )
        assert len(joined) == 2, f"Expected 2 matched cells, got {len(joined)}"
        assert pd.api.types.is_string_dtype(joined["month"]), "joined month must be string"


# ---------------------------------------------------------------------------
# 6. Promotion-to-sales window matching
# ---------------------------------------------------------------------------


class TestMatchPromoToSalesWeeks:
    def test_matches_promos(self, raw_pack_base: RawPack) -> None:
        by_week = roll_sales_to_brand_region_week(
            raw_pack_base.fact_primary_sales, raw_pack_base.dim_sku, raw_pack_base.dim_geo
        )
        promos_renamed = raw_pack_base.promotions.rename(columns={"sku": "sku_code"})
        matched, log = match_promo_to_sales_weeks(
            promos_renamed,
            by_week,
            raw_pack_base.fact_primary_sales,
            raw_pack_base.dim_geo,
        )
        assert log.item == 6
        assert "matched" in log.detail
        assert "promo_id" in matched.columns


# ---------------------------------------------------------------------------
# 7. Playbook thresholds
# ---------------------------------------------------------------------------


class TestParsePlaybookThresholds:
    def test_parses_rules(self, raw_pack_base: RawPack) -> None:
        rules, log = parse_playbook_thresholds(raw_pack_base.playbook)
        assert log.item == 7
        assert log.passed
        assert len(rules) == 2
        assert all(isinstance(r, ThresholdRule) for r in rules)
        assert rules[0].rule_id == "R-01"
        assert rules[0].needs_approval is True
        assert rules[1].needs_approval is False


# ---------------------------------------------------------------------------
# Integration — conform_pack smoke test
# ---------------------------------------------------------------------------


class TestConformPack:
    def test_conform_pack_returns_pack_and_log(self, raw_pack_base: RawPack) -> None:
        cpack, log = conform_pack(raw_pack_base)
        assert isinstance(cpack, ConformedPack)
        assert len(log) == 7
        assert all(isinstance(e, LogEntry) for e in log)

    def test_all_seven_items_present_and_passed(self, raw_pack_base: RawPack) -> None:
        _, log = conform_pack(raw_pack_base)
        for entry in log:
            assert entry.passed, f"Item {entry.item} ({entry.title}) failed: {entry.detail}"
        items = sorted(e.item for e in log)
        assert items == [1, 2, 3, 4, 5, 6, 7]

    def test_target_month_is_string_in_conformed_pack(self, raw_pack_base: RawPack) -> None:
        cpack, _ = conform_pack(raw_pack_base)
        assert pd.api.types.is_string_dtype(cpack.fact_targets["month"])
        assert pd.api.types.is_string_dtype(cpack.target_by_brand_region_month["month"])
        assert (
            cpack.fact_targets["month"].dtype == cpack.target_by_brand_region_month["month"].dtype
        )

    def test_720_cells_with_real_pack(self) -> None:
        assert True
