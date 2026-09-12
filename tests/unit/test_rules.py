"""Threshold behaviour of the eight playbook rules, on data built to sit either side of each.

The provided pack exercises six of the eight. R-02 and R-05 have no qualifying case on it,
and the design commits to reporting that rather than moving a threshold until something
appears. Proving both fire on synthetic data is what earns the right to report zero: without
these tests, "no case" and "dead code" look identical.
"""

from __future__ import annotations

import datetime as dt

import pytest

from acpl_assistant.actions import rules

WEEKS = ["2026-01-06", "2026-01-13", "2026-01-20", "2026-01-27"]


def _achievement(ratio: float, *, month: str = "2026-01", brand: str = "BrandA") -> list[tuple]:
    """One cell at exactly *ratio*: a round target keeps the ratio free of float drift."""
    target = 1_000_000
    return [(month, brand, "North", target * ratio, target)]


def _stockout_weeks(count: int, *, sku: str = "K-1", month: str = "2026-01") -> list[tuple]:
    return [("D1", sku, f"{month}-{(week * 7) + 1:02d}", "North", 3) for week in range(count)]


def _flat_sales(value: float, weeks: list[str], sku: str = "K-1") -> list[tuple]:
    return [(sku, "T-N", week, 10, value) for week in weeks]


class TestR01SupplyConstrained:
    """Achievement below 70% with repeated stock-outs on the brand's SKUs in the region."""

    def test_fires_below_the_threshold_with_repeated_stockouts(self, synthetic) -> None:
        con = synthetic(achievement=_achievement(0.699), stockouts=_stockout_weeks(2))
        found = rules.evaluate_r01(con)
        assert len(found) == 1
        assert found[0].entity == ("BrandA", "North")
        assert found[0].figures["stockout_weeks"] == 2

    def test_silent_exactly_at_the_threshold(self, synthetic) -> None:
        con = synthetic(achievement=_achievement(0.70), stockouts=_stockout_weeks(2))
        assert rules.evaluate_r01(con) == []

    def test_silent_when_a_single_week_is_out(self, synthetic) -> None:
        """One stock-out week is not 'repeated', so supply is not yet the likely cause."""
        con = synthetic(achievement=_achievement(0.50), stockouts=_stockout_weeks(1))
        assert rules.evaluate_r01(con) == []

    def test_silent_with_no_stockout_at_all(self, synthetic) -> None:
        con = synthetic(achievement=_achievement(0.50))
        assert rules.evaluate_r01(con) == []

    def test_stockouts_on_another_brand_do_not_count(self, synthetic) -> None:
        con = synthetic(
            achievement=_achievement(0.60),
            stockouts=_stockout_weeks(3, sku="K-3"),  # K-3 is BrandB
        )
        assert rules.evaluate_r01(con) == []


class TestR02WeakPromotion:
    """A miss below 80% while an overlapping promotion delivers under 10% uplift."""

    PROMO = [("P-1", "K-1", "North", "2026-01-06", "2026-01-27", 10, "Price-off")]
    BASELINE = ["2025-12-02", "2025-12-09", "2025-12-16", "2025-12-23"]

    def test_fires_on_weak_uplift(self, synthetic) -> None:
        con = synthetic(
            achievement=_achievement(0.79),
            promotions=self.PROMO,
            sales=_flat_sales(100.0, self.BASELINE) + _flat_sales(105.0, WEEKS),
        )
        found = rules.evaluate_r02(con)
        assert len(found) == 1
        assert found[0].figures["uplift_pct"] == 5.0
        assert found[0].figures["promo_id"] == "P-1"

    def test_silent_when_uplift_reaches_ten_percent(self, synthetic) -> None:
        con = synthetic(
            achievement=_achievement(0.79),
            promotions=self.PROMO,
            sales=_flat_sales(100.0, self.BASELINE) + _flat_sales(110.0, WEEKS),
        )
        assert rules.evaluate_r02(con) == []

    def test_silent_when_the_brand_is_not_missing_target(self, synthetic) -> None:
        con = synthetic(
            achievement=_achievement(0.80),
            promotions=self.PROMO,
            sales=_flat_sales(100.0, self.BASELINE) + _flat_sales(105.0, WEEKS),
        )
        assert rules.evaluate_r02(con) == []

    def test_silent_with_no_promotion(self, synthetic) -> None:
        con = synthetic(achievement=_achievement(0.79))
        assert rules.evaluate_r02(con) == []


class TestR03AndR06Unexplained:
    """The same miss, split by whether a note covers the brand, region and month."""

    NOTE = [
        (
            "visit_note.docx",
            "BrandA lost shelf space in the North during January 2026.",
            "BrandA",
            "North",
            "",
            "January",
            "2026-01",
        )
    ]

    def test_r03_fires_when_a_note_covers_the_cell(self, synthetic) -> None:
        con = synthetic(achievement=_achievement(0.79), documents=self.NOTE)
        found = rules.evaluate_r03(con)
        assert len(found) == 1
        assert found[0].figures["supporting_notes"] == 1
        assert found[0].evidence[-1]["source_file"] == "visit_note.docx"

    def test_r06_fires_when_no_note_covers_the_cell(self, synthetic) -> None:
        con = synthetic(achievement=_achievement(0.79))
        found = rules.evaluate_r06(con)
        assert len(found) == 1
        assert found[0].figures["supporting_notes"] == 0

    def test_the_two_are_mutually_exclusive(self, synthetic) -> None:
        """Whatever the facts, a cell may satisfy one of them but never both."""
        for documents in ([], self.NOTE):
            con = synthetic(achievement=_achievement(0.79), documents=documents)
            fired = {f.entity for f in rules.evaluate_r03(con)} & {
                f.entity for f in rules.evaluate_r06(con)
            }
            assert fired == set()

    def test_a_note_for_another_month_does_not_count(self, synthetic) -> None:
        """A note resolves to a month only where a year was available; unresolved is not a match."""
        unresolved = [(*self.NOTE[0][:6], "")]
        con = synthetic(achievement=_achievement(0.79), documents=unresolved)
        assert rules.evaluate_r03(con) == []
        assert len(rules.evaluate_r06(con)) == 1

    def test_a_stockout_sends_the_cell_elsewhere(self, synthetic) -> None:
        con = synthetic(achievement=_achievement(0.79), stockouts=_stockout_weeks(1))
        assert rules.evaluate_r03(con) == []
        assert rules.evaluate_r06(con) == []


class TestR04ChronicStockout:
    """One distributor out of stock on one SKU for more than six weeks."""

    @staticmethod
    def _weeks(count: int) -> list[tuple]:
        start = dt.date(2026, 1, 6)
        return [
            ("D1", "K-1", (start + dt.timedelta(weeks=w)).isoformat(), "North", 5)
            for w in range(count)
        ]

    def test_fires_above_six_weeks(self, synthetic) -> None:
        con = synthetic(stockouts=self._weeks(7))
        found = rules.evaluate_r04(con)
        assert len(found) == 1
        assert found[0].figures["weeks_out"] == 7
        assert found[0].entity == ("D1", "K-1")

    def test_silent_at_exactly_six_weeks(self, synthetic) -> None:
        assert rules.evaluate_r04(synthetic(stockouts=self._weeks(6))) == []

    def test_repeated_rows_in_one_week_are_one_week(self, synthetic) -> None:
        """Seven rows in a single week is not seven weeks out of stock."""
        repeated = [("D1", "K-1", "2026-01-06", "North", 5) for _ in range(7)]
        assert rules.evaluate_r04(synthetic(stockouts=repeated)) == []

    def test_carries_no_rupee_figure(self, synthetic) -> None:
        """The stock-out log holds days, not value; no rupee proxy is invented for it."""
        found = rules.evaluate_r04(synthetic(stockouts=self._weeks(7)))
        assert found[0].value_inr is None


class TestR05OverDelivery:
    """Achievement above 110% — the rule with no case on the provided pack."""

    def test_fires_above_the_threshold(self, synthetic) -> None:
        con = synthetic(achievement=_achievement(1.101))
        found = rules.evaluate_r05(con)
        assert len(found) == 1
        assert found[0].figures["surplus_inr"] == 101_000

    def test_silent_exactly_at_the_threshold(self, synthetic) -> None:
        assert rules.evaluate_r05(synthetic(achievement=_achievement(1.10))) == []

    def test_silent_below_the_threshold(self, synthetic) -> None:
        assert rules.evaluate_r05(synthetic(achievement=_achievement(1.09))) == []


class TestR07StrongUplift:
    """A promotion whose uplift beat 25% against its four preceding weeks."""

    PROMO = [("P-1", "K-1", "North", "2026-01-06", "2026-01-27", 10, "Buy 2 Get 1")]
    BASELINE = ["2025-12-02", "2025-12-09", "2025-12-16", "2025-12-23"]

    def _con(self, synthetic, promo_value: float):
        return synthetic(
            promotions=self.PROMO,
            sales=_flat_sales(100.0, self.BASELINE) + _flat_sales(promo_value, WEEKS),
        )

    def test_fires_above_the_threshold(self, synthetic) -> None:
        found = rules.evaluate_r07(self._con(synthetic, 126.0))
        assert len(found) == 1
        assert found[0].figures["uplift_pct"] == 26.0
        assert found[0].figures["baseline_weeks"] == 4

    def test_silent_exactly_at_the_threshold(self, synthetic) -> None:
        assert rules.evaluate_r07(self._con(synthetic, 125.0)) == []

    def test_unmeasurable_promotion_is_absent_not_zero(self, synthetic) -> None:
        """A promotion opening in the first week of the ledger has no baseline to beat."""
        con = synthetic(promotions=self.PROMO, sales=_flat_sales(500.0, WEEKS))
        assert rules.promotion_uplift(con) == []
        assert rules.evaluate_r07(con) == []

    def test_baseline_is_four_weeks_not_four_rows(self, synthetic) -> None:
        """Two territories per region: taking four rows would take two weeks of trading.

        Both territories trade at 100 a week, so the region trades at 200. A promotion
        week at 260 is a 30% uplift. Reading the baseline as four *rows* would compare
        260 against 100 and report 160%.
        """
        baseline = [
            (sku, terr, week, 10, 100.0)
            for sku, terr in (("K-1", "T-N"), ("K-1", "T-N2"))
            for week in self.BASELINE
        ]
        promo = [("K-1", terr, week, 10, 130.0) for terr in ("T-N", "T-N2") for week in WEEKS]
        con = synthetic(promotions=self.PROMO, sales=baseline + promo)
        con.execute("INSERT INTO dim_geo VALUES ('T-N2', 'North Two', 'North', 'Delhi')")
        assert rules.promotion_uplift(con)[0]["uplift"] == pytest.approx(0.3)


class TestR08DistributorLevel:
    """A distributor short of three or more distinct SKUs within one month."""

    @staticmethod
    def _skus(count: int) -> list[tuple]:
        codes = ["K-1", "K-2", "K-3"]
        return [("D1", codes[i], "2026-01-06", "North", 4) for i in range(count)]

    def test_fires_at_three_distinct_skus(self, synthetic) -> None:
        found = rules.evaluate_r08(synthetic(stockouts=self._skus(3)))
        assert len(found) == 1
        assert found[0].figures["skus_out"] == 3
        assert found[0].entity == ("D1",)

    def test_silent_at_two(self, synthetic) -> None:
        assert rules.evaluate_r08(synthetic(stockouts=self._skus(2))) == []

    def test_one_sku_out_three_times_is_not_three_skus(self, synthetic) -> None:
        repeated = [("D1", "K-1", f"2026-01-{d:02d}", "North", 4) for d in (6, 13, 20)]
        assert rules.evaluate_r08(synthetic(stockouts=repeated)) == []

    def test_months_are_counted_separately(self, synthetic) -> None:
        """Three SKUs spread over two months is not three SKUs in a month."""
        spread = [
            ("D1", "K-1", "2026-01-06", "North", 4),
            ("D1", "K-2", "2026-01-13", "North", 4),
            ("D1", "K-3", "2026-02-03", "North", 4),
        ]
        assert rules.evaluate_r08(synthetic(stockouts=spread)) == []


class TestRegistry:
    def test_every_rule_is_registered_once(self) -> None:
        assert list(rules.RULES) == [f"R-{n:02d}" for n in range(1, 9)]

    def test_each_spec_names_its_own_rule(self) -> None:
        assert all(rule_id == spec.rule_id for rule_id, spec in rules.RULES.items())

    def test_evaluate_all_covers_the_registry(self, synthetic) -> None:
        con = synthetic(
            achievement=_achievement(0.60) + _achievement(1.2, month="2026-02"),
            stockouts=_stockout_weeks(3),
        )
        fired = {f.rule_id for f in rules.evaluate_all(con)}
        assert fired == {"R-01", "R-05"}


class TestHelpers:
    def test_month_bounds_handles_december(self) -> None:
        start, end = rules._month_bounds("2025-12")
        assert (start.isoformat(), end.isoformat()) == ("2025-12-01", "2025-12-31")

    def test_month_bounds_handles_february(self) -> None:
        start, end = rules._month_bounds("2026-02")
        assert (start.isoformat(), end.isoformat()) == ("2026-02-01", "2026-02-28")

    def test_tagged_splits_and_drops_blanks(self) -> None:
        assert rules._tagged("North,  West , ") == {"North", "West"}
        assert rules._tagged(None) == set()

    def test_excerpt_is_bounded(self, synthetic) -> None:
        long_note = [
            (
                "long.docx",
                "BrandA North " + ("word " * 400),
                "BrandA",
                "North",
                "",
                "January",
                "2026-01",
            )
        ]
        con = synthetic(achievement=_achievement(0.79), documents=long_note)
        excerpt = rules.evaluate_r03(con)[0].evidence[-1]["excerpt"]
        assert len(excerpt) <= rules.EXCERPT_CHARS + 1
        assert excerpt.endswith("…")


class TestValueNormalisation:
    """Dates reach the caller as ``YYYY-MM-DD`` whichever temporal type DuckDB returns."""

    def test_a_timestamp_loses_its_time(self) -> None:
        assert rules._normalise(dt.datetime(2026, 4, 14, 9, 30)) == "2026-04-14"  # noqa: DTZ001

    def test_a_plain_date_passes_through(self) -> None:
        assert rules._normalise(dt.date(2026, 4, 14)) == "2026-04-14"

    def test_other_values_are_untouched(self) -> None:
        assert rules._normalise(9) == 9
        assert rules._normalise("D032") == "D032"
        assert rules._normalise(None) is None
