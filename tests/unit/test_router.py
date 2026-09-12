"""The router's schema and its re-validation of what the model returned.

No provider is called here. What is tested is the boundary: the model answers inside a
closed schema, and every field it returns is checked against the catalogue again on this
side of the wire. A model that answers off schema should change the grain of an answer at
worst; it must never reach the query builder with a value of its own invention.
"""

from __future__ import annotations

from typing import Any

import pytest

from acpl_assistant.ask.intents import (
    DIRECTIONS,
    INTENT_IDS,
    INTENTS,
    METRICS,
    NO_INTENT,
    TOP_N_DEFAULT,
    TOP_N_MAX,
    TOP_N_MIN,
)
from acpl_assistant.ask.resolve import FISCAL_QUARTERS, Entities, Period
from acpl_assistant.ask.router import (
    ALL_DIMENSIONS,
    PREMISES,
    ROUTE_SCHEMA,
    _catalogue,
    _clean_confidence,
    _clean_dimension,
    _clean_intent,
    _clean_top_n,
    build_user_message,
    route,
)
from acpl_assistant.llm.client import LLMResult, Usage

Q4 = Period(label="FY26 Q4", months=FISCAL_QUARTERS[4])
Q3 = Period(label="FY26 Q3", months=FISCAL_QUARTERS[3])

VALID: dict[str, Any] = {
    "intent": "Q2",
    "confidence": 0.8,
    "premise": "none",
    "metric": "units",
    "dimension": "region",
    "direction": "smallest",
    "top_n": 3,
}


class StubClient:
    """Returns one fixed payload, so only the re-validation is under test."""

    model = "stub"

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.user = ""

    def complete_json(self, *, system: str, user: str, **_: Any) -> LLMResult:
        self.user = user
        return LLMResult(data=self.data, usage=Usage(1, 1, 2), model=self.model, cost_usd=0.0)


def routed(data: dict[str, Any], compare_to: Period | None = None):
    """Route one stubbed response and return the re-validated decision."""
    decision, _ = route(StubClient(data), "a question", Entities(), Q4, compare_to)
    return decision


# ---------------------------------------------------------------------------
# The published schema
# ---------------------------------------------------------------------------


class TestSchema:
    def test_every_property_is_required(self) -> None:
        """Gemini's strict mode needs one response shape, not a family of them."""
        assert set(ROUTE_SCHEMA["required"]) == set(ROUTE_SCHEMA["properties"])
        assert ROUTE_SCHEMA["additionalProperties"] is False

    def test_the_intent_enum_is_the_catalogue_plus_none(self) -> None:
        assert ROUTE_SCHEMA["properties"]["intent"]["enum"] == [*INTENT_IDS, NO_INTENT]

    def test_the_closed_enums_match_their_modules(self) -> None:
        properties = ROUTE_SCHEMA["properties"]
        assert properties["metric"]["enum"] == list(METRICS)
        assert properties["direction"]["enum"] == list(DIRECTIONS)
        assert properties["premise"]["enum"] == list(PREMISES)

    def test_the_dimension_enum_is_every_breakdown_any_intent_offers(self) -> None:
        offered = {name for spec in INTENTS.values() for name in spec.dimensions}
        assert set(ALL_DIMENSIONS) == offered
        assert "" in ROUTE_SCHEMA["properties"]["dimension"]["enum"]


class TestCatalogue:
    def test_every_intent_reaches_the_prompt(self) -> None:
        """Generated from the catalogue, so a new intent cannot be missing from it."""
        rendered = _catalogue()
        for spec in INTENTS.values():
            assert spec.intent in rendered
            assert spec.example in rendered

    def test_a_ranked_family_says_what_it_ranks_by(self) -> None:
        rendered = _catalogue()
        for spec in INTENTS.values():
            if spec.ranks_on:
                assert spec.ranks_on in rendered


# ---------------------------------------------------------------------------
# Re-validation
# ---------------------------------------------------------------------------


class TestCleaners:
    @pytest.mark.parametrize("value", ["Q1", "q1", " q1 ", "Q8"])
    def test_a_catalogue_intent_is_accepted_however_it_is_cased(self, value: str) -> None:
        assert _clean_intent(value) in INTENT_IDS

    @pytest.mark.parametrize("value", ["Q9", "", None, 7, "SELECT"])
    def test_anything_else_is_no_intent_at_all(self, value: Any) -> None:
        assert _clean_intent(value) == NO_INTENT

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(0, TOP_N_MIN), (-5, TOP_N_MIN), (3, 3), (999, TOP_N_MAX), ("x", TOP_N_DEFAULT)],
    )
    def test_the_row_count_is_clamped_not_trusted(self, value: Any, expected: int) -> None:
        assert _clean_top_n(value) == expected

    def test_a_missing_row_count_takes_the_default(self) -> None:
        assert _clean_top_n(None) == TOP_N_DEFAULT

    def test_a_breakdown_the_intent_cannot_produce_is_dropped(self) -> None:
        """Q4 has no "category", so asking for one falls back to Q4's own default."""
        assert _clean_dimension("Q4", "category") == ""
        assert _clean_dimension("Q2", "category") == "category"

    def test_a_breakdown_for_an_unknown_intent_is_dropped(self) -> None:
        assert _clean_dimension("Q9", "brand") == ""

    @pytest.mark.parametrize(
        ("value", "expected"), [(0.5, 0.5), (2.0, 1.0), (-1, 0.0), ("x", 0.0), (None, 0.0)]
    )
    def test_confidence_is_read_into_zero_to_one(self, value: Any, expected: float) -> None:
        assert _clean_confidence(value) == expected


class TestRoute:
    def test_a_valid_response_passes_through_intact(self) -> None:
        decision = routed(VALID)
        assert decision.intent == "Q2"
        assert decision.routed
        assert decision.slots.metric == "units"
        assert decision.slots.dimension == "region"
        assert decision.slots.direction == "smallest"
        assert decision.slots.top_n == 3
        assert decision.raw == VALID

    def test_an_off_schema_response_falls_back_rather_than_passing_through(self) -> None:
        decision = routed(
            {**VALID, "metric": "rupees", "direction": "bottom", "premise": "sideways"}
        )
        assert decision.slots.metric == "value"
        assert decision.slots.direction == "largest"
        assert decision.premise == "none"

    def test_an_empty_response_is_no_route(self) -> None:
        decision = routed({})
        assert decision.intent == NO_INTENT
        assert not decision.routed

    def test_the_second_period_is_only_carried_into_a_comparison(self) -> None:
        """Q3 is the one family that has anything to compare against."""
        assert routed({**VALID, "intent": "Q3"}, compare_to=Q3).slots.compare_to == Q3
        assert routed({**VALID, "intent": "Q2"}, compare_to=Q3).slots.compare_to is None

    def test_the_resolved_entities_and_period_are_the_ones_used(self) -> None:
        entities = Entities(brands=["Aqualite"])
        decision, _ = route(StubClient(VALID), "q", entities, Q4)
        assert decision.slots.entities is entities
        assert decision.slots.period is Q4

    def test_low_confidence_does_not_gate_execution(self) -> None:
        """The numeric verifier catches the failure that matters; confidence only records."""
        decision = routed({**VALID, "confidence": 0.01})
        assert decision.routed
        assert decision.confidence == 0.01


class TestUserMessage:
    def test_resolved_entities_are_restated_rather_than_re_inferred(self) -> None:
        message = build_user_message("How did Aqualite do?", Entities(brands=["Aqualite"]), Q4)
        assert "brands: Aqualite" in message
        assert "FY26 Q4" in message

    def test_a_question_naming_nothing_says_so(self) -> None:
        assert "none named" in build_user_message("How did we do?", Entities(), Q4)

    def test_the_question_travels_verbatim(self) -> None:
        assert "Which SKUs?" in build_user_message("Which SKUs?", Entities(), Q4)
