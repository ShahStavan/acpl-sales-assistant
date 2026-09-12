"""Scope resolution, grouping by target entity, ranking, and approval gating.

These are the decisions that turn findings into a list a manager acts on, so each is tested
against the behaviour the design commits to rather than against the shape of the code.
"""

from __future__ import annotations

import datetime as dt
import random

import pytest

from acpl_assistant.actions import rules
from acpl_assistant.actions.engine import (
    SCOPE_ALL,
    GroupedFinding,
    _normalise_scope,
    group_findings,
    load_playbook,
    rank_key,
    resolve_scope,
    run_actions,
)

ALL_RULES = [f"R-{n:02d}" for n in range(1, 9)]
GATED_RULES = {"R-01", "R-04", "R-08"}


def _finding(
    rule_id: str = "R-06",
    entity: tuple[str, ...] = ("BrandA", "North"),
    *,
    region: str = "North",
    month: str = "2026-01",
    value_inr: float | None = 1000.0,
    magnitude: float = 1.0,
    month_grain: bool = True,
) -> rules.Finding:
    start, end = rules._month_bounds(month)
    return rules.Finding(
        rule_id=rule_id,
        entity=entity,
        entity_label=" / ".join(entity),
        region=region,
        period_label=month,
        period_start=start,
        period_end=end,
        month_grain=month_grain,
        figures={
            "month": month,
            "achievement_pct": 70,
            "shortfall_inr": int(value_inr or 0),
        },
        evidence=[{"source_file": "fact_targets.csv", "month": month}],
        value_inr=value_inr,
        magnitude=magnitude,
    )


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


class TestScopeResolution:
    @pytest.mark.parametrize(
        ("scope", "expected"),
        [
            ("West", "West"),
            ("west", "West"),
            ("WEST", "West"),
            ("  west  ", "West"),
            ("west region", "West"),
            ("West Region", "West"),
            ("North India", "North"),
            ("all", SCOPE_ALL),
            ("ALL", SCOPE_ALL),
        ],
    )
    def test_resolves(self, synthetic, scope: str, expected: str) -> None:
        assert resolve_scope(synthetic(), scope) == expected

    @pytest.mark.parametrize(
        "scope",
        ["", "   ", "Atlantis", "BrandA", "D032", "region", "the west", "wets"],
    )
    def test_withholds(self, synthetic, scope: str) -> None:
        """Anything that is not a held region or 'all' resolves to nothing (DESIGN.md §5.5)."""
        assert resolve_scope(synthetic(), scope) is None

    def test_a_near_miss_is_not_fuzzily_matched(self, synthetic) -> None:
        """Resolving 'Wets' to West would answer for a region nobody asked about."""
        con = synthetic(achievement=[("2026-01", "BrandA", "West", 500_000, 1_000_000)])
        assert run_actions(con, "Wets") == []
        assert run_actions(con, "West") != []

    def test_normalisation_leaves_a_bare_region_alone(self) -> None:
        assert _normalise_scope("South") == "south"

    def test_normalisation_strips_only_trailing_words(self) -> None:
        assert _normalise_scope("region west") == "region west"


# ---------------------------------------------------------------------------
# Playbook
# ---------------------------------------------------------------------------


class TestPlaybookGating:
    def test_state_matches_the_provided_playbook(self, synthetic) -> None:
        playbook = load_playbook(synthetic())
        gated = {rule_id for rule_id, e in playbook.items() if e.state == "PENDING_APPROVAL"}
        assert gated == GATED_RULES

    def test_state_follows_the_spreadsheet_not_the_code(self, synthetic) -> None:
        """Invert every approval flag: every state must invert with it.

        This is the test that proves §5.6 — ``state`` is read, not inferred. If the rule
        ids were hard-coded anywhere in the engine, this would still report R-01 gated.
        """
        inverted = [(rid, "act", rid not in GATED_RULES, "cond") for rid in ALL_RULES]
        playbook = load_playbook(synthetic(playbook=inverted))
        gated = {rule_id for rule_id, e in playbook.items() if e.state == "PENDING_APPROVAL"}
        assert gated == set(ALL_RULES) - GATED_RULES

    def test_action_text_is_quoted_from_the_playbook(self, synthetic) -> None:
        custom = [(rid, f"do {rid}", False, "cond") for rid in ALL_RULES]
        con = synthetic(
            achievement=[("2026-01", "BrandA", "North", 500_000, 1_000_000)],
            playbook=custom,
        )
        assert run_actions(con, "all")[0].action == "do R-06"


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


class TestGrouping:
    def test_one_entity_across_three_months_is_one_action(self) -> None:
        group = [_finding(month=m) for m in ("2026-04", "2026-05", "2026-06")]
        grouped = group_findings(group)
        assert len(grouped) == 1
        assert grouped[0].period == "2026-04..2026-06"

    def test_a_single_month_reads_as_that_month(self) -> None:
        assert group_findings([_finding(month="2026-02")])[0].period == "2026-02"

    def test_different_entities_stay_apart(self) -> None:
        group = [_finding(entity=("BrandA", "North")), _finding(entity=("BrandB", "North"))]
        assert len(group_findings(group)) == 2

    def test_the_same_entity_under_two_rules_stays_apart(self) -> None:
        group = [_finding(rule_id="R-03"), _finding(rule_id="R-06")]
        assert len(group_findings(group)) == 2

    def test_week_grain_periods_keep_their_dates(self) -> None:
        first = _finding(month="2026-04", month_grain=False)
        second = _finding(month="2026-06", month_grain=False)
        assert group_findings([first, second])[0].period == "2026-04-01..2026-06-30"

    def test_rupee_magnitudes_add_up(self) -> None:
        group = [
            _finding(month="2026-04", value_inr=100.0),
            _finding(month="2026-05", value_inr=250.0),
        ]
        assert group_findings(group)[0].value_inr == 350.0

    def test_a_group_with_no_rupee_figure_stays_none(self) -> None:
        group = [_finding(value_inr=None), _finding(month="2026-02", value_inr=None)]
        assert group_findings(group)[0].value_inr is None

    def test_repeated_evidence_rows_appear_once(self) -> None:
        group = [_finding(month="2026-01"), _finding(month="2026-01")]
        assert len(group_findings(group)[0].evidence) == 1

    def test_recency_is_the_latest_period_in_the_group(self) -> None:
        group = [_finding(month=m) for m in ("2026-01", "2026-06", "2026-03")]
        assert group_findings(group)[0].recency == dt.date(2026, 6, 30)


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def _grouped(
    rule_id: str = "R-06",
    entity: tuple[str, ...] = ("BrandA", "North"),
    *,
    recency: dt.date = dt.date(2026, 1, 31),
    value_inr: float | None = 1000.0,
    magnitude: float = 1.0,
) -> GroupedFinding:
    return GroupedFinding(
        rule_id=rule_id,
        entity=entity,
        entity_label=" / ".join(entity),
        region="North",
        period="2026-01",
        recency=recency,
        figures={},
        evidence=[],
        value_inr=value_inr,
        magnitude=magnitude,
    )


class TestRanking:
    def test_the_more_recent_finding_comes_first(self) -> None:
        older = _grouped(entity=("Old",), recency=dt.date(2025, 7, 31), value_inr=9_000_000.0)
        newer = _grouped(entity=("New",), recency=dt.date(2026, 6, 30), value_inr=1.0)
        assert [g.entity for g in sorted([older, newer], key=rank_key)] == [("New",), ("Old",)]

    def test_within_a_period_the_larger_sum_comes_first(self) -> None:
        small = _grouped(entity=("Small",), value_inr=10.0)
        large = _grouped(entity=("Large",), value_inr=1000.0)
        assert [g.entity for g in sorted([small, large], key=rank_key)] == [("Large",), ("Small",)]

    def test_a_rule_with_no_rupee_figure_ranks_after_one_that_has_it(self) -> None:
        priced = _grouped(entity=("Priced",), value_inr=1.0)
        unpriced = _grouped(entity=("Unpriced",), value_inr=None, magnitude=99.0)
        order = [g.entity for g in sorted([unpriced, priced], key=rank_key)]
        assert order == [("Priced",), ("Unpriced",)]

    def test_magnitude_breaks_a_tie_between_unpriced_rules(self) -> None:
        mild = _grouped(entity=("Mild",), value_inr=None, magnitude=3.0)
        severe = _grouped(entity=("Severe",), value_inr=None, magnitude=9.0)
        order = [g.entity for g in sorted([mild, severe], key=rank_key)]
        assert order == [("Severe",), ("Mild",)]

    def test_the_order_is_total(self) -> None:
        """Identical on every ranked dimension: the order must still be deterministic."""
        items = [_grouped(rule_id="R-03", entity=(f"E{i}",)) for i in range(20)]
        shuffled = list(items)
        random.Random(0).shuffle(shuffled)  # noqa: S311 — reordering input, not seeding crypto
        assert sorted(items, key=rank_key) == sorted(shuffled, key=rank_key)


# ---------------------------------------------------------------------------
# End to end over a synthetic warehouse
# ---------------------------------------------------------------------------


class TestRunActions:
    ACHIEVEMENT = [
        ("2026-01", "BrandA", "North", 500_000, 1_000_000),
        ("2026-02", "BrandB", "West", 600_000, 1_000_000),
    ]

    def test_priority_is_a_dense_one_based_rank(self, synthetic) -> None:
        items = run_actions(synthetic(achievement=self.ACHIEVEMENT), "all")
        assert [i.priority for i in items] == list(range(1, len(items) + 1))

    def test_scope_partitions_the_full_list(self, synthetic) -> None:
        con = synthetic(achievement=self.ACHIEVEMENT)
        everything = {(i.rule_id, i.finding) for i in run_actions(con, "all")}
        per_region = {
            (i.rule_id, i.finding)
            for region in ("North", "South", "East", "West")
            for i in run_actions(con, region)
        }
        assert everything == per_region

    def test_an_empty_warehouse_reports_nothing_rather_than_filler(self, synthetic) -> None:
        assert run_actions(synthetic(), "all") == []

    def test_an_unresolvable_scope_returns_an_empty_list(self, synthetic) -> None:
        assert run_actions(synthetic(achievement=self.ACHIEVEMENT), "Atlantis") == []

    def test_the_same_warehouse_gives_the_same_list_twice(self, synthetic) -> None:
        con = synthetic(achievement=self.ACHIEVEMENT)
        first = [i.model_dump() for i in run_actions(con, "all")]
        second = [i.model_dump() for i in run_actions(con, "all")]
        assert first == second


class TestTheSilentRulesRenderCorrectly:
    """R-02 and R-05 have no case on the provided pack, so only synthetic data reaches
    their grouping and wording. Without this, the two would be implemented but never run
    end to end, which is the difference between "no case" and "untested".
    """

    BASELINE = ["2025-12-02", "2025-12-09", "2025-12-16", "2025-12-23"]
    PROMO_WEEKS = ["2026-01-06", "2026-01-13", "2026-01-20", "2026-01-27"]
    PROMO = [("P-1", "K-1", "North", "2026-01-06", "2026-01-27", 10, "Price-off")]

    def test_r02_produces_a_complete_action(self, synthetic) -> None:
        con = synthetic(
            achievement=[("2026-01", "BrandA", "North", 790_000, 1_000_000)],
            promotions=self.PROMO,
            sales=[("K-1", "T-N", week, 10, 100.0) for week in self.BASELINE]
            + [("K-1", "T-N", week, 10, 105.0) for week in self.PROMO_WEEKS],
        )
        action = next(a for a in run_actions(con, "all") if a.rule_id == "R-02")
        assert action.state == "RECOMMENDED"
        assert action.period == "2026-01"
        assert "79%" in action.finding
        assert "5.0% uplift" in action.finding
        assert "210,000" in action.finding

    def test_r02_groups_three_months_and_quotes_the_weakest_uplift(self, synthetic) -> None:
        con = synthetic(
            achievement=[
                (month, "BrandA", "North", 790_000, 1_000_000)
                for month in ("2026-01", "2026-02", "2026-03")
            ],
            promotions=[
                ("P-1", "K-1", "North", "2026-01-06", "2026-01-27", 10, "Price-off"),
                ("P-2", "K-1", "North", "2026-02-03", "2026-02-24", 10, "Price-off"),
                ("P-3", "K-1", "North", "2026-03-03", "2026-03-24", 10, "Price-off"),
            ],
            sales=[("K-1", "T-N", week, 10, 100.0) for week in self.BASELINE]
            + [("K-1", "T-N", week, 10, 105.0) for week in self.PROMO_WEEKS]
            + [("K-1", "T-N", f"2026-02-{d:02d}", 10, 108.0) for d in (3, 10, 17, 24)]
            + [("K-1", "T-N", f"2026-03-{d:02d}", 10, 102.0) for d in (3, 10, 17, 24)],
        )
        actions = [a for a in run_actions(con, "all") if a.rule_id == "R-02"]
        assert len(actions) == 1
        assert actions[0].period == "2026-01..2026-03"
        assert "630,000" in actions[0].finding

    def test_r05_produces_a_complete_action(self, synthetic) -> None:
        con = synthetic(achievement=[("2026-01", "BrandA", "North", 1_150_000, 1_000_000)])
        action = next(a for a in run_actions(con, "all") if a.rule_id == "R-05")
        assert action.state == "RECOMMENDED"
        assert "115%" in action.finding
        assert "150,000 above plan" in action.finding

    def test_r05_reports_a_band_when_the_months_differ(self, synthetic) -> None:
        con = synthetic(
            achievement=[
                ("2026-01", "BrandA", "North", 1_150_000, 1_000_000),
                ("2026-02", "BrandA", "North", 1_200_000, 1_000_000),
            ]
        )
        action = next(a for a in run_actions(con, "all") if a.rule_id == "R-05")
        assert "115–120%" in action.finding
        assert action.period == "2026-01..2026-02"
