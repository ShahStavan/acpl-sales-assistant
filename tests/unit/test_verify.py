"""The verifier: which numerals an answer is entitled to state, and which premises hold.

This is the stage that makes an accuracy number meaningful. Everything upstream constrains
what the model is *asked* to do; this is the only thing that checks what it actually did,
and it checks structurally rather than by asking a second model whether the first behaved.
"""

from __future__ import annotations

from typing import Any

import pytest

from acpl_assistant.ask.intents import Slots
from acpl_assistant.ask.resolve import FISCAL_QUARTERS, FULL_YEAR, Entities, Period
from acpl_assistant.ask.verify import (
    FALSE_PREMISE,
    UNGROUNDED_FIGURE,
    check_premise,
    grounded_values,
    ungrounded_figures,
    verify,
)

Q4 = Period(label="FY26 Q4", months=FISCAL_QUARTERS[4])
Q3 = Period(label="FY26 Q3", months=FISCAL_QUARTERS[3])


def slots(**overrides: Any) -> Slots:
    """Slots with the fields a verifier reads, and nothing more."""
    base: dict[str, Any] = {"entities": Entities(), "period": Q4}
    return Slots(**{**base, **overrides})


ROW = {
    "source_file": "fact_targets.csv",
    "brand": "Aqualite",
    "region": "West",
    "gap_value_inr": 1912659,
    "achievement_ratio": 0.8577,
    "achievement_pct": 86,
}


# ---------------------------------------------------------------------------
# What grounds a figure
# ---------------------------------------------------------------------------


class TestGroundedValues:
    def test_every_number_in_a_row_grounds(self) -> None:
        values = grounded_values([ROW], slots())
        assert {1912659.0, 0.8577, 86.0} <= values

    def test_digits_inside_a_string_ground_too(self) -> None:
        """A date, an id or a finding sentence carries digits a reader will see quoted."""
        rows = [{"source_file": "x", "finding": "D032 was out for 53 days in 2026-04"}]
        values = grounded_values(rows, slots())
        assert {32.0, 53.0, 2026.0, 4.0} <= values

    def test_the_period_and_its_months_ground(self) -> None:
        values = grounded_values([], slots())
        assert 26.0 in values  # from the FY26 Q4 label
        assert 2026.04 not in values
        assert 4.0 in values

    def test_both_periods_ground_in_a_comparison(self) -> None:
        values = grounded_values([], slots(period=Q4, compare_to=Q3))
        assert {1.0, 3.0, 4.0} <= values

    def test_a_negative_figure_grounds_its_magnitude_too(self) -> None:
        """Prose says sales "fell by 1,985,290"; the row says ``-1985290``.

        The numeral extractor reads digits and cannot match a minus sign, so without the
        magnitude a declining comparison could never state its own figure.
        """
        rows = [{"delta_value_inr": -1985290, "change_pct": -17}]
        values = grounded_values(rows, slots())
        assert {1985290.0, 17.0} <= values
        assert {-1985290.0, -17.0} <= values

    def test_a_decline_can_be_written_the_way_english_writes_it(self) -> None:
        rows = [{"from_value_inr": 11369222, "to_value_inr": 9383932, "delta_value_inr": -1985290}]
        answer = "Sales fell by 1,985,290, from 11,369,222 to 9,383,932."
        assert ungrounded_figures(answer, grounded_values(rows, slots())) == []

    def test_a_named_entity_grounds_its_own_digits(self) -> None:
        entities = Entities(pack_sizes=["1L"], skus=["BV-0104"])
        assert {1.0, 104.0} <= grounded_values([], slots(entities=entities))

    def test_the_row_count_grounds(self) -> None:
        """ "Three regions missed target" states a figure the rows themselves supply."""
        assert 3.0 in grounded_values([ROW, ROW, ROW], slots())

    def test_top_n_grounds(self) -> None:
        assert 7.0 in grounded_values([], slots(top_n=7))

    def test_booleans_and_nulls_ground_nothing(self) -> None:
        rows = [{"source_file": "x", "flag": True, "nothing": None}]
        assert grounded_values(rows, None) == {1.0}  # only the row count

    def test_extra_values_can_be_supplied(self) -> None:
        assert 42.0 in grounded_values([], None, extra=["42 widgets"])


# ---------------------------------------------------------------------------
# What an answer may write
# ---------------------------------------------------------------------------


class TestUngroundedFigures:
    def test_a_copied_figure_passes(self) -> None:
        assert ungrounded_figures("The gap was INR 1912659.", {1912659.0}) == []

    def test_thousands_separators_are_read_through(self) -> None:
        assert ungrounded_figures("INR 1,912,659", {1912659.0}) == []

    def test_a_rounded_figure_is_a_restatement(self) -> None:
        """93 may stand for 92.77: fewer decimals is still the same figure."""
        assert ungrounded_figures("achievement was 93%", {92.77}) == []

    def test_a_rescaled_figure_is_arithmetic(self) -> None:
        """2.2 may not stand for 22002083 - rescaling is a computation, and those are SQL's."""
        assert ungrounded_figures("about 2.2 crore", {22002083.0}) == ["2.2"]

    def test_an_invented_figure_is_reported(self) -> None:
        assert ungrounded_figures("The gap was 999.", {1912659.0}) == ["999"]

    def test_each_ungrounded_numeral_is_reported_once(self) -> None:
        assert ungrounded_figures("999 and 999 and 888", set()) == ["999", "888"]

    def test_a_list_marker_is_layout_not_a_figure(self) -> None:
        answer = "1. Aqualite\n2. SparkClean\n3) FreshFloor"
        assert ungrounded_figures(answer, set()) == []

    def test_a_figure_after_a_list_marker_is_still_checked(self) -> None:
        assert ungrounded_figures("1. Aqualite at INR 55", set()) == ["55"]

    def test_prose_with_no_numerals_is_always_grounded(self) -> None:
        assert ungrounded_figures("No brand missed its target.", set()) == []


# ---------------------------------------------------------------------------
# Premise
# ---------------------------------------------------------------------------


class TestPremise:
    def test_none_is_never_contradicted(self) -> None:
        assert check_premise("none", [{"delta_value_inr": -5}]) is None

    def test_asserted_growth_against_a_fall_is_corrected(self) -> None:
        assert "fell" in (check_premise("growth", [{"delta_value_inr": -5}]) or "")

    def test_asserted_decline_against_a_rise_is_corrected(self) -> None:
        assert "grew" in (check_premise("decline", [{"delta_value_inr": 5}]) or "")

    def test_a_premise_the_rows_confirm_passes(self) -> None:
        assert check_premise("growth", [{"delta_value_inr": 5}]) is None
        assert check_premise("decline", [{"delta_value_inr": -5}]) is None

    def test_the_change_ratio_settles_it_when_there_is_no_delta(self) -> None:
        assert check_premise("growth", [{"change_ratio": -0.12}]) is not None

    def test_the_leading_row_decides_rather_than_the_sum(self) -> None:
        """Summing a ranking of changes would let opposite movements cancel into "flat"."""
        rows = [{"delta_value_inr": -100}, {"delta_value_inr": 100}]
        assert check_premise("growth", rows) is not None

    def test_a_flat_period_contradicts_nothing(self) -> None:
        assert check_premise("growth", [{"delta_value_inr": 0}]) is None

    def test_rows_that_cannot_settle_it_are_silent(self) -> None:
        """A trend word routed to a single-period ranking has no change column to be wrong."""
        assert check_premise("growth", [{"value_inr": 5}]) is None
        assert check_premise("growth", []) is None

    def test_an_asserted_miss_against_a_beat_is_corrected(self) -> None:
        assert "met or beat" in (check_premise("miss", [{"achievement_ratio": 1.04}]) or "")

    def test_an_asserted_beat_against_a_miss_is_corrected(self) -> None:
        assert "missed" in (check_premise("beat", [{"achievement_ratio": 0.86}]) or "")

    def test_exactly_on_target_counts_as_meeting_it(self) -> None:
        assert check_premise("miss", [{"achievement_ratio": 1.0}]) is not None
        assert check_premise("beat", [{"achievement_ratio": 1.0}]) is None

    def test_rows_without_an_achievement_ratio_are_silent(self) -> None:
        assert check_premise("miss", [{"value_inr": 1}]) is None
        assert check_premise("beat", []) is None


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------


class TestVerify:
    def test_a_grounded_answer_passes(self) -> None:
        verdict = verify("Aqualite in West is short by INR 1912659.", [ROW], slots())
        assert verdict.ok
        assert verdict.reason == ""

    def test_an_invented_figure_is_withheld_and_named(self) -> None:
        verdict = verify("The gap was INR 5000000.", [ROW], slots())
        assert not verdict.ok
        assert verdict.reason == UNGROUNDED_FIGURE
        assert verdict.ungrounded == ("5000000",)
        assert "5000000" in verdict.message

    def test_the_premise_is_checked_before_the_figures(self) -> None:
        """An answer built on a false assumption is wrong even when every figure is real."""
        rows = [{"source_file": "x", "delta_value_inr": -5}]
        verdict = verify("It fell by 5.", rows, slots(), premise="growth")
        assert verdict.reason == FALSE_PREMISE

    def test_a_period_the_answer_names_is_not_an_invention(self) -> None:
        assert verify("In FY26 Q4 the gap was 1912659.", [ROW], slots()).ok

    @pytest.mark.parametrize("period", [FULL_YEAR, Q3, Q4])
    def test_every_period_label_grounds_itself(self, period: Period) -> None:
        assert verify(f"Over {period.label} nothing changed.", [], slots(period=period)).ok
