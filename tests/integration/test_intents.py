"""Every intent, every breakdown, against the real warehouse.

The router can ask for any breakdown its chosen family lists, and the executor has to
answer all of them. These tests drive the whole matrix — eight families times every
dimension, both metrics, both directions — so a query shape that only ever gets exercised
by an unusual question is not first run in front of a user.

What is asserted is structural: the SQL runs, the rows carry a traceable source file, the
ranking is actually ordered, and rupees and ratios are shaped the way the verifier expects.
The figures themselves are checked in ``eval/questions.yaml``, against hand-written SQL.
"""

from __future__ import annotations

from collections.abc import Iterator
import itertools
from pathlib import Path
from typing import Any

import duckdb
import pytest

from acpl_assistant.ask.execute import ACTION_LIMIT, NO_ROUTE, NO_ROWS, execute, shape_row
from acpl_assistant.ask.intents import (
    DIRECTIONS,
    INTENT_IDS,
    INTENTS,
    METRICS,
    TOP_N_MAX,
    Slots,
    spec_for,
)
from acpl_assistant.ask.resolve import FISCAL_QUARTERS, FULL_YEAR, Entities, Period

pytestmark = pytest.mark.integration

Q3 = Period(label="FY26 Q3", months=FISCAL_QUARTERS[3])
Q4 = Period(label="FY26 Q4", months=FISCAL_QUARTERS[4])


@pytest.fixture(scope="module")
def con(prepared: tuple[Path, dict]) -> Iterator[duckdb.DuckDBPyConnection]:
    """A read-only handle on the warehouse the session fixture built."""
    db_path, _ = prepared
    handle = duckdb.connect(str(db_path), read_only=True)
    yield handle
    handle.close()


def slots(**overrides: Any) -> Slots:
    """Slots over the full year with nothing named, unless the caller says otherwise."""
    base: dict[str, Any] = {"entities": Entities(), "period": FULL_YEAR}
    return Slots(**{**base, **overrides})


# Every (intent, dimension) pair the router could produce, including the empty dimension
# that makes each family fall back to its own default.
MATRIX = [
    (intent, dimension) for intent in INTENT_IDS for dimension in ("", *INTENTS[intent].dimensions)
]


class TestEveryQueryShape:
    @pytest.mark.parametrize(("intent", "dimension"), MATRIX)
    def test_it_runs_and_returns_traceable_rows(
        self, con: duckdb.DuckDBPyConnection, intent: str, dimension: str
    ) -> None:
        executed = execute(con, intent, slots(dimension=dimension, compare_to=Q3))
        assert executed.refusal is None, f"{intent}/{dimension}: {executed.refusal}"
        assert executed.rows
        assert all(row["source_file"] for row in executed.rows)

    @pytest.mark.parametrize(
        ("intent", "metric", "direction"),
        list(itertools.product(("Q1", "Q2", "Q3", "Q4", "Q5"), METRICS, DIRECTIONS)),
    )
    def test_both_metrics_and_both_directions_are_answerable(
        self, con: duckdb.DuckDBPyConnection, intent: str, metric: str, direction: str
    ) -> None:
        executed = execute(con, intent, slots(metric=metric, direction=direction, compare_to=Q3))
        assert executed.refusal is None
        assert executed.rows

    @pytest.mark.parametrize("intent", ["Q1", "Q2", "Q3", "Q4", "Q5"])
    def test_the_direction_actually_reverses_the_ranking(
        self, con: duckdb.DuckDBPyConnection, intent: str
    ) -> None:
        """Largest and smallest must not be the same query with a different label."""
        largest = execute(con, intent, slots(direction="largest", compare_to=Q3)).rows
        smallest = execute(con, intent, slots(direction="smallest", compare_to=Q3)).rows
        assert largest and smallest
        assert largest[0] != smallest[0]

    @pytest.mark.parametrize("intent", ["Q1", "Q2", "Q3", "Q4", "Q5"])
    def test_top_n_bounds_the_ranking(self, con: duckdb.DuckDBPyConnection, intent: str) -> None:
        assert len(execute(con, intent, slots(top_n=2, compare_to=Q3)).rows) <= 2

    def test_a_ranking_is_ordered_by_the_measure_it_ranks_on(
        self, con: duckdb.DuckDBPyConnection
    ) -> None:
        rows = execute(con, "Q1", slots(period=Q4, top_n=TOP_N_MAX)).rows
        gaps = [row["gap_value_inr"] for row in rows]
        assert gaps == sorted(gaps, reverse=True)


class TestFilters:
    def test_a_named_brand_narrows_every_family_that_accepts_one(
        self, con: duckdb.DuckDBPyConnection
    ) -> None:
        named = slots(entities=Entities(brands=["Aqualite"]))
        for intent in ("Q1", "Q2", "Q4", "Q5"):
            rows = execute(con, intent, named).rows
            assert rows, intent
            brands = {row.get("brand") for row in rows if "brand" in row}
            assert brands <= {"Aqualite"}, intent

    def test_a_named_region_narrows_the_sales_family(self, con: duckdb.DuckDBPyConnection) -> None:
        rows = execute(
            con, "Q2", slots(entities=Entities(regions=["West"]), dimension="region")
        ).rows
        assert {row["region"] for row in rows} == {"West"}

    def test_a_filter_a_family_cannot_honour_is_refused_rather_than_ignored(
        self, con: duckdb.DuckDBPyConnection
    ) -> None:
        """Answering would report a wider total than the question asked for."""
        named = slots(entities=Entities(distributors=["D032"]))
        executed = execute(con, "Q2", named)
        assert executed.refusal is not None
        assert executed.refusal.reason == NO_ROUTE
        assert "distributor" in executed.refusal.message

    def test_an_unknown_intent_has_no_execution_path(self, con: duckdb.DuckDBPyConnection) -> None:
        executed = execute(con, "Q9", slots())
        assert executed.refusal is not None
        assert executed.refusal.reason == NO_ROUTE

    def test_a_period_the_data_does_not_reach_is_no_rows(
        self, con: duckdb.DuckDBPyConnection
    ) -> None:
        empty = Period(label="FY26 M13", months=("2026-13",))
        executed = execute(con, "Q2", slots(period=empty))
        assert executed.refusal is not None
        assert executed.refusal.reason == NO_ROWS
        assert "FY26 M13" in executed.refusal.message

    def test_the_refusal_names_what_the_question_named(
        self, con: duckdb.DuckDBPyConnection
    ) -> None:
        empty = Period(label="FY26 M13", months=("2026-13",))
        executed = execute(con, "Q2", slots(period=empty, entities=Entities(brands=["Aqualite"])))
        assert "Aqualite" in executed.refusal.message


class TestQ7:
    def test_a_region_scopes_the_actions(self, con: duckdb.DuckDBPyConnection) -> None:
        executed = execute(con, "Q7", slots(entities=Entities(regions=["West"])))
        assert executed.refusal is None
        assert 0 < len(executed.actions) <= ACTION_LIMIT

    def test_a_territory_widens_to_its_region(self, con: duckdb.DuckDBPyConnection) -> None:
        """The playbook works on regions; a question naming a city still has to land."""
        by_city = execute(con, "Q7", slots(entities=Entities(territories=["Mumbai"])))
        by_region = execute(con, "Q7", slots(entities=Entities(regions=["West"])))
        assert by_city.rows == by_region.rows

    def test_every_action_row_names_the_playbook(self, con: duckdb.DuckDBPyConnection) -> None:
        executed = execute(con, "Q7", slots(entities=Entities(regions=["West"])))
        summaries = [row for row in executed.rows if "rule_id" in row]
        assert summaries
        assert all(row["source_file"] == "action_playbook.xlsx" for row in summaries)

    def test_the_evidence_behind_each_finding_travels_with_it(
        self, con: duckdb.DuckDBPyConnection
    ) -> None:
        executed = execute(con, "Q7", slots(entities=Entities(regions=["West"])))
        assert len(executed.rows) > len(executed.actions)


class TestRowShaping:
    def test_rupees_are_whole(self) -> None:
        shaped = shape_row({"value_inr": 22002082.560000002}, "x.csv")
        assert shaped["value_inr"] == 22002083

    def test_a_ratio_keeps_four_places_and_gains_a_percentage(self) -> None:
        """Without the percentage column, every percent in the prose would look invented."""
        shaped = shape_row({"achievement_ratio": 0.857712}, "x.csv")
        assert shaped["achievement_ratio"] == 0.8577
        assert shaped["achievement_pct"] == 86

    def test_uplift_is_a_ratio_even_without_the_suffix(self) -> None:
        shaped = shape_row({"uplift": 0.2185929648241205}, "x.csv")
        assert shaped["uplift"] == 0.2186
        assert shaped["uplift_pct"] == 22

    def test_a_row_that_names_its_own_source_keeps_it(self) -> None:
        shaped = shape_row({"source_file": "documents/", "n": 1}, "fallback.csv")
        assert shaped["source_file"] == "documents/"

    def test_non_numeric_columns_pass_through_untouched(self) -> None:
        shaped = shape_row({"brand": "Aqualite", "flag": True, "nothing": None}, "x.csv")
        assert shaped["brand"] == "Aqualite"
        assert shaped["flag"] is True
        assert shaped["nothing"] is None

    def test_a_count_is_not_rounded_into_a_rupee_figure(self) -> None:
        shaped = shape_row({"units": 156364}, "x.csv")
        assert shaped["units"] == 156364


class TestCatalogueIntegrity:
    def test_every_family_has_a_summary_and_an_example(self) -> None:
        for spec in INTENTS.values():
            assert spec.summary
            assert spec.example

    def test_only_the_actions_family_delegates_its_query(self) -> None:
        for intent, spec in INTENTS.items():
            assert (spec.build is None) == (intent == "Q7"), intent

    def test_a_ranked_family_says_what_it_ranks_on(self) -> None:
        for intent in ("Q1", "Q2", "Q3", "Q4", "Q5"):
            assert INTENTS[intent].ranks_on

    def test_every_default_breakdown_is_one_the_family_offers(self) -> None:
        for spec in INTENTS.values():
            if spec.default_dimension:
                assert spec.default_dimension in spec.dimensions, spec.intent

    def test_an_unknown_intent_has_no_spec(self) -> None:
        assert spec_for("Q9") is None
