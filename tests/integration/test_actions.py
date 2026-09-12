"""The actions engine against the prepared warehouse, with every published figure pinned.

Each assertion here is a number DESIGN.md §5.3 states in print: the rules that fire, the
entities they fire on, the achievement ratios and the counts. A change in the data or in a
threshold fails this suite rather than quietly rewriting a table a reviewer has read.

Nothing in this module calls a model or touches the network. The engine is code only, which
is what makes these figures reproducible.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import re
from typing import Any

import duckdb
import pytest

from acpl_assistant.actions import rules
from acpl_assistant.actions.engine import group_findings, load_playbook, run_actions

pytestmark = pytest.mark.integration

# DESIGN.md §5.3 — the actions this warehouse yields, after grouping by target entity.
EXPECTED_ITEMS = 55
EXPECTED_BY_RULE = {"R-01": 1, "R-03": 1, "R-04": 2, "R-06": 1, "R-07": 23, "R-08": 27}
SILENT_RULES = ("R-02", "R-05")
GATED_RULES = {"R-01", "R-04", "R-08"}
REGIONS = ("North", "South", "East", "West")

# Findings before grouping — 3 months of R-01, 47 distributor-months of R-08.
EXPECTED_FINDINGS_BY_RULE = {"R-01": 3, "R-03": 1, "R-04": 2, "R-06": 1, "R-07": 23, "R-08": 47}

# The single promotion of forty with no prior period to measure against.
UNMEASURABLE_PROMOTION = "PR-2025-056"
MEASURABLE_PROMOTIONS = 39


@pytest.fixture(scope="module")
def warehouse(prepared: tuple[Path, dict]) -> duckdb.DuckDBPyConnection:
    """A read-only connection to the warehouse the session fixture prepared."""
    db_path, _ = prepared
    con = duckdb.connect(str(db_path), read_only=True)
    yield con
    con.close()


@pytest.fixture(scope="module")
def all_actions(warehouse: duckdb.DuckDBPyConnection) -> list:
    return run_actions(warehouse, "all")


def _one(actions: list, rule_id: str):
    matching = [a for a in actions if a.rule_id == rule_id]
    assert len(matching) == 1, f"{rule_id} produced {len(matching)} actions, expected 1"
    return matching[0]


class TestWhatFires:
    def test_the_whole_list_is_fifty_five_actions(self, all_actions: list) -> None:
        assert len(all_actions) == EXPECTED_ITEMS

    def test_each_rule_fires_the_published_number_of_times(self, all_actions: list) -> None:
        assert dict(Counter(a.rule_id for a in all_actions)) == EXPECTED_BY_RULE

    @pytest.mark.parametrize("rule_id", SILENT_RULES)
    def test_the_two_silent_rules_report_nothing(
        self, warehouse: duckdb.DuckDBPyConnection, rule_id: str
    ) -> None:
        """No qualifying case on this data. The threshold is not moved to produce one."""
        assert rules.RULES[rule_id].evaluate(warehouse) == []

    def test_findings_before_grouping(self, warehouse: duckdb.DuckDBPyConnection) -> None:
        counts = Counter(f.rule_id for f in rules.evaluate_all(warehouse))
        assert dict(counts) == EXPECTED_FINDINGS_BY_RULE

    def test_r05_is_silent_because_nothing_reaches_the_threshold(
        self, warehouse: duckdb.DuckDBPyConnection
    ) -> None:
        """State the reason as a figure: the best cell in FY26 is 108.7%, not above 110%."""
        best = warehouse.execute("SELECT max(achievement_ratio) FROM v_achievement").fetchone()[0]
        assert round(best * 100, 1) == 108.7
        assert best < rules.ACHIEVEMENT_OVER


class TestR01Aqualite:
    def test_it_is_aqualite_in_the_west_across_three_months(self, all_actions: list) -> None:
        action = _one(all_actions, "R-01")
        assert action.period == "2026-04..2026-06"
        assert "Aqualite in the West" in action.finding
        assert action.state == "PENDING_APPROVAL"

    def test_all_three_months_sit_at_sixty_two_percent(
        self, warehouse: duckdb.DuckDBPyConnection
    ) -> None:
        found = rules.evaluate_r01(warehouse)
        assert [f.figures["month"] for f in found] == ["2026-04", "2026-05", "2026-06"]
        assert {f.figures["achievement_pct"] for f in found} == {62}

    def test_it_cites_the_stock_outs_that_make_supply_the_cause(
        self, warehouse: duckdb.DuckDBPyConnection
    ) -> None:
        found = rules.evaluate_r01(warehouse)
        assert [f.figures["stockout_weeks"] for f in found] == [3, 4, 2]

    def test_its_evidence_names_all_three_sources(self, all_actions: list) -> None:
        files = {row.source_file for row in _one(all_actions, "R-01").evidence}
        assert files == {"fact_primary_sales.csv", "fact_targets.csv", "stockouts.csv"}


class TestR03AndR06Discrimination:
    """The one place a document changes which rule fires, so the split is asserted directly."""

    def test_r03_is_cremedelight_in_the_north_in_february(self, all_actions: list) -> None:
        action = _one(all_actions, "R-03")
        assert action.period == "2026-02"
        assert "CremeDelight in the North" in action.finding
        assert "72%" in action.finding

    def test_r03_cites_the_visit_note(self, all_actions: list) -> None:
        files = {row.source_file for row in _one(all_actions, "R-03").evidence}
        assert "visit_note_north_feb2026.docx" in files

    def test_r06_is_mintguard_in_the_east_in_march(self, all_actions: list) -> None:
        action = _one(all_actions, "R-06")
        assert action.period == "2026-03"
        assert "MintGuard in the East" in action.finding
        assert "74%" in action.finding

    def test_r06_cites_no_document(self, all_actions: list) -> None:
        """There is no note, and none is manufactured: 'no cause is determinable'."""
        files = {row.source_file for row in _one(all_actions, "R-06").evidence}
        assert not any(f.endswith(".docx") for f in files)

    def test_the_two_never_name_the_same_cell(self, warehouse: duckdb.DuckDBPyConnection) -> None:
        r03 = {(f.entity, f.period_label) for f in rules.evaluate_r03(warehouse)}
        r06 = {(f.entity, f.period_label) for f in rules.evaluate_r06(warehouse)}
        assert r03 & r06 == set()


class TestR04Chronic:
    def test_two_distributors_on_one_sku(self, all_actions: list) -> None:
        found = sorted((a for a in all_actions if a.rule_id == "R-04"), key=lambda a: a.finding)
        assert len(found) == 2
        assert all("BV-0104" in a.finding for a in found)
        assert all(a.state == "PENDING_APPROVAL" for a in found)

    def test_both_ran_nine_weeks_over_the_same_window(
        self, warehouse: duckdb.DuckDBPyConnection
    ) -> None:
        found = rules.evaluate_r04(warehouse)
        assert {f.entity for f in found} == {("D032", "BV-0104"), ("D033", "BV-0104")}
        assert {f.figures["weeks_out"] for f in found} == {9}
        assert {f.period_label for f in found} == {"2026-04-14..2026-06-09"}


class TestR07Promotions:
    def test_twenty_three_promotions_beat_the_threshold(self, all_actions: list) -> None:
        assert sum(1 for a in all_actions if a.rule_id == "R-07") == 23

    def test_thirty_nine_of_forty_are_measurable(
        self, warehouse: duckdb.DuckDBPyConnection
    ) -> None:
        measured = rules.promotion_uplift(warehouse)
        assert len(measured) == MEASURABLE_PROMOTIONS
        assert UNMEASURABLE_PROMOTION not in {p["promo_id"] for p in measured}

    def test_the_observed_uplift_range(self, warehouse: duckdb.DuckDBPyConnection) -> None:
        """APPROACH.md §F publishes this range as the reason R-02 has no case."""
        uplifts = [p["uplift"] for p in rules.promotion_uplift(warehouse)]
        assert round(min(uplifts) * 100, 1) == 13.5
        assert round(max(uplifts) * 100, 1) == 44.7

    def test_no_promotion_is_weak_enough_for_r02(
        self, warehouse: duckdb.DuckDBPyConnection
    ) -> None:
        uplifts = [p["uplift"] for p in rules.promotion_uplift(warehouse)]
        assert min(uplifts) > rules.UPLIFT_WEAK

    def test_the_baseline_is_four_weeks_wherever_four_exist(
        self, warehouse: duckdb.DuckDBPyConnection
    ) -> None:
        weeks = Counter(p["baseline_weeks"] for p in rules.promotion_uplift(warehouse))
        assert weeks == Counter({4: 37, 3: 1, 2: 1})


class TestR08Distributors:
    def test_forty_seven_distributor_months_become_twenty_seven_calls(
        self, warehouse: duckdb.DuckDBPyConnection, all_actions: list
    ) -> None:
        findings = rules.evaluate_r08(warehouse)
        assert len(findings) == 47
        assert len({f.entity for f in findings}) == 27
        assert sum(1 for a in all_actions if a.rule_id == "R-08") == 27

    def test_every_call_is_approval_gated(self, all_actions: list) -> None:
        assert all(a.state == "PENDING_APPROVAL" for a in all_actions if a.rule_id == "R-08")


class TestApprovalGating:
    def test_exactly_three_rules_are_gated(self, all_actions: list) -> None:
        gated = {a.rule_id for a in all_actions if a.state == "PENDING_APPROVAL"}
        assert gated == GATED_RULES & set(EXPECTED_BY_RULE)

    def test_the_rest_are_recommended(self, all_actions: list) -> None:
        loose = {a.rule_id for a in all_actions if a.state == "RECOMMENDED"}
        assert loose == set(EXPECTED_BY_RULE) - GATED_RULES

    def test_the_flag_comes_from_the_spreadsheet(
        self, warehouse: duckdb.DuckDBPyConnection
    ) -> None:
        playbook = load_playbook(warehouse)
        assert {r for r, e in playbook.items() if e.needs_approval} == GATED_RULES

    def test_the_action_text_is_the_playbook_wording(self, all_actions: list) -> None:
        wording = {a.action for a in all_actions if a.rule_id == "R-04"}
        assert wording == {"Raise a replenishment order for that distributor"}


class TestScope:
    def test_all_regions_together_are_the_whole_list(
        self, warehouse: duckdb.DuckDBPyConnection, all_actions: list
    ) -> None:
        """No action is lost between regions, and none is counted twice."""
        per_region = [item for region in REGIONS for item in run_actions(warehouse, region)]
        assert len(per_region) == len(all_actions)
        assert {(i.rule_id, i.finding) for i in per_region} == {
            (i.rule_id, i.finding) for i in all_actions
        }

    def test_a_region_reads_the_same_however_it_is_written(
        self, warehouse: duckdb.DuckDBPyConnection
    ) -> None:
        canonical = [i.finding for i in run_actions(warehouse, "West")]
        for spelling in ("west", "WEST", " west region ", "West India"):
            assert [i.finding for i in run_actions(warehouse, spelling)] == canonical

    def test_an_unknown_scope_is_withheld(self, warehouse: duckdb.DuckDBPyConnection) -> None:
        for scope in ("Atlantis", "Aqualite", "D032", ""):
            assert run_actions(warehouse, scope) == []

    def test_the_west_holds_the_supply_constrained_finding(
        self, warehouse: duckdb.DuckDBPyConnection
    ) -> None:
        assert "R-01" in {i.rule_id for i in run_actions(warehouse, "West")}
        assert "R-01" not in {i.rule_id for i in run_actions(warehouse, "North")}


class TestRankingAndShape:
    def test_priority_is_a_dense_rank_from_one(self, all_actions: list) -> None:
        assert [a.priority for a in all_actions] == list(range(1, EXPECTED_ITEMS + 1))

    def test_the_supply_constrained_finding_leads_the_list(self, all_actions: list) -> None:
        """Most recent, and the largest sum at risk within that period."""
        assert all_actions[0].rule_id == "R-01"

    def test_the_list_is_not_capped(self, all_actions: list) -> None:
        assert sum(EXPECTED_BY_RULE.values()) == len(all_actions)

    def test_two_runs_agree_exactly(self, warehouse: duckdb.DuckDBPyConnection) -> None:
        first = [a.model_dump() for a in run_actions(warehouse, "all")]
        second = [a.model_dump() for a in run_actions(warehouse, "all")]
        assert first == second

    def test_every_action_carries_evidence_and_a_period(self, all_actions: list) -> None:
        for action in all_actions:
            assert action.evidence, f"{action.rule_id} {action.finding}"
            assert action.period
            assert all(row.source_file for row in action.evidence)


# ---------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set[float]:
    return {float(match.replace(",", "")) for match in _NUMBER.findall(text)}


def _grounded_numbers(*values: Any) -> set[float]:
    """Every number a value makes available, whether it is numeric or embedded in text."""
    found: set[float] = set()
    for value in values:
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int | float):
            found.add(float(value))
        elif isinstance(value, str):
            found |= _numbers(value)
        elif isinstance(value, dict):
            found |= _grounded_numbers(*value.values())
        elif isinstance(value, list | tuple):
            found |= _grounded_numbers(*value)
    return found


class TestFindingTextIsGrounded:
    """No figure reaches a manager that is not also in the rows behind it.

    This is the §3.3 verifier contract applied to the actions path. The engine writes its
    findings from templates rather than from a model, so the property should hold by
    construction — which is exactly why it is worth asserting: a template that starts
    computing a number of its own would break it.
    """

    def test_every_numeral_in_every_finding_appears_in_its_evidence(
        self, warehouse: duckdb.DuckDBPyConnection
    ) -> None:
        for item in group_findings(rules.evaluate_all(warehouse)):
            spec = rules.RULES[item.rule_id]
            finding = spec.summarise(item.entity_label, item.period, item.figures)
            grounded = _grounded_numbers(
                item.figures, item.evidence, item.period, item.entity_label
            )
            ungrounded = _numbers(finding) - grounded
            assert not ungrounded, f"{item.rule_id} {item.entity}: {ungrounded} in {finding!r}"

    def test_the_check_would_catch_an_invented_figure(self) -> None:
        """Prove the assertion above can fail, rather than passing vacuously."""
        grounded = _grounded_numbers({"weeks_out": 9}, "2026-04")
        assert _numbers("out of stock in 9 weeks from 2026-04") - grounded == set()
        assert _numbers("costing INR 4,200,000") - grounded == {4_200_000.0}
