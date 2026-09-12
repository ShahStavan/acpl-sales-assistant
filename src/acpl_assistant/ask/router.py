"""Route a resolved question to one intent and its query shape (LLM call 1).

The model makes one decision here and only one: which of Q1–Q8 the question belongs to, and
what shape the query should take. It never sees a figure, never names an entity and never
picks a period — the entities and the period were resolved from the warehouse's own
vocabularies before this call, in :mod:`acpl_assistant.ask.resolve` (DESIGN.md §3.4).

Every field the model returns is drawn from a closed enum fixed in this file, and every one
is re-checked here against the catalogue before it reaches SQL. A model that answers off
schema, or names a breakdown the chosen intent cannot produce, changes the grain of the
answer at worst; it cannot reach the query builder with a value of its own invention.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any

from acpl_assistant.ask.intents import (
    DIRECTIONS,
    INTENT_IDS,
    INTENTS,
    METRICS,
    NO_INTENT,
    TOP_N_DEFAULT,
    TOP_N_MAX,
    TOP_N_MIN,
    Slots,
    spec_for,
)

if TYPE_CHECKING:
    from acpl_assistant.ask.resolve import Entities, Period
    from acpl_assistant.llm.client import LLMClient, LLMResult

logger = logging.getLogger(__name__)

SCHEMA_NAME = "acpl_route"

# What the question asserts about direction of travel, for the false-premise check that
# runs after execution. "none" is the ordinary case: most questions assert nothing.
PREMISES = ("growth", "decline", "miss", "beat", "none")

# Every breakdown any intent offers, as one enum. Each intent accepts the subset it can
# produce and falls back to its own default for the rest, so one enum serves all eight
# without letting a Q4 breakdown reach a Q2 query.
ALL_DIMENSIONS: tuple[str, ...] = tuple(
    dict.fromkeys(name for spec in INTENTS.values() for name in spec.dimensions)
)

ROUTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": [*INTENT_IDS, NO_INTENT],
            "description": "The single question family this belongs to, or NONE.",
        },
        "confidence": {
            "type": "number",
            "description": "0 to 1, how certain the intent choice is.",
        },
        "premise": {
            "type": "string",
            "enum": list(PREMISES),
            "description": "What the question asserts as already true, if anything.",
        },
        "metric": {"type": "string", "enum": list(METRICS)},
        "dimension": {
            "type": "string",
            "enum": [*ALL_DIMENSIONS, ""],
            "description": "Breakdown to group by; empty to use the intent's default.",
        },
        "direction": {
            "type": "string",
            "enum": list(DIRECTIONS),
            "description": "Order by the family's ranking measure: largest first, "
            "or smallest first.",
        },
        "top_n": {"type": "integer", "description": "How many rows the answer needs."},
    },
    # Gemini's strict mode requires every property to be required and additionalProperties
    # to be false, so there is exactly one response shape rather than a family of them.
    "required": ["intent", "confidence", "premise", "metric", "dimension", "direction", "top_n"],
    "additionalProperties": False,
}


def _catalogue() -> str:
    """Render the intent catalogue for the prompt, from the catalogue itself.

    Generated rather than written out, so an intent added to
    :mod:`acpl_assistant.ask.intents` cannot be missing from the prompt that routes to it.
    """
    lines = []
    for spec in INTENTS.values():
        breakdowns = ", ".join(spec.dimensions) if spec.dimensions else "not applicable"
        entry = (
            f"{spec.intent} — {spec.summary}\n"
            f'    example: "{spec.example}"\n'
            f"    breakdowns: {breakdowns}"
        )
        if spec.ranks_on:
            entry += f"\n    ranked by: {spec.ranks_on}"
        lines.append(entry)
    return "\n".join(lines)


SYSTEM_PROMPT = f"""\
You classify a sales question against a fixed catalogue of eight question families for \
ACPL, an Indian FMCG distributor. You do not answer the question and you never state a \
figure.

The catalogue:

{_catalogue()}

Rules:
- Choose exactly one family, or NONE if the question fits none of them. NONE is correct \
far more often than forcing a poor fit: a question the catalogue cannot serve is refused, \
which is the intended outcome.
- Q3 only when the question compares two distinct periods. A single period with a trend \
word in it ("how is X doing") is Q2.
- Q7 only when the question asks what to do, not merely what happened.
- "premise" records what the question takes for granted before any data is read — \
"growth" or "decline" for an asserted rise or fall, "miss" or "beat" for an asserted \
result against target, "none" when the question asserts nothing. Most questions are "none".
- "metric" is units only when the question asks about volume, cases or units; \
otherwise value.
- "direction" orders by the measure the family is ranked by, not by whether the result is \
good news. The worst distributor has the largest days out of stock, so "which distributors \
were worst" is largest. Where we are "losing most" against target is the largest shortfall.
- "dimension" must be one of the breakdowns listed for the family you chose. Leave it \
empty if none fits.
- "top_n" is how many rows the answer needs: the number the question asks for, or {TOP_N_DEFAULT} \
when it does not say.

Return only the JSON object.\
"""


@dataclass(frozen=True)
class Route:
    """One routing decision, after every field has been re-checked against the catalogue."""

    intent: str
    slots: Slots
    premise: str
    confidence: float
    raw: dict[str, Any]
    """The model's response as returned, for the request log and the evaluation set."""

    @property
    def routed(self) -> bool:
        """Whether the question reached a family that can be executed."""
        return self.intent in INTENT_IDS


def _clean_intent(value: Any) -> str:
    """Accept only an intent the catalogue holds; anything else is no route at all."""
    text = str(value or "").strip().upper()
    return text if text in INTENT_IDS else NO_INTENT


def _clean_choice(value: Any, allowed: tuple[str, ...], fallback: str) -> str:
    """Accept only a value from a closed set, falling back rather than passing it through."""
    text = str(value or "").strip().casefold()
    return text if text in allowed else fallback


def _clean_top_n(value: Any) -> int:
    """Clamp the row count, treating anything unreadable as the default."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return TOP_N_DEFAULT
    return max(TOP_N_MIN, min(number, TOP_N_MAX))


def _clean_dimension(intent: str, value: Any) -> str:
    """Keep a breakdown only if the chosen intent can actually produce it."""
    spec = spec_for(intent)
    text = str(value or "").strip().casefold()
    if spec is None or text not in spec.dimensions:
        return ""
    return text


def _clean_confidence(value: Any) -> float:
    """Read the model's confidence, defaulting to zero when it is unreadable."""
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        return 0.0


def build_user_message(question: str, entities: Entities, period: Period) -> str:
    """Describe the already-resolved question to the router.

    The entities and the period are stated rather than left to be inferred: they were
    matched against the warehouse's vocabularies, so restating them keeps the model from
    re-deciding something code has already settled correctly.
    """
    named = entities.as_dict()
    if named:
        resolved = "; ".join(f"{family}: {', '.join(values)}" for family, values in named.items())
    else:
        resolved = "none named"
    return (
        f"Question: {question}\n"
        f"Entities already resolved from the data: {resolved}\n"
        f"Period already resolved: {period.label}"
    )


def route(
    client: LLMClient,
    question: str,
    entities: Entities,
    period: Period,
    compare_to: Period | None = None,
) -> tuple[Route, LLMResult]:
    """Ask the model which family the question belongs to and in what shape.

    Returns the re-validated route alongside the raw provider result, so the caller can
    meter the call's own tokens and cost rather than an estimate of them.

    Raises:
        LLMError: propagated from the client, so the pipeline reports a provider failure as
            a refusal with its reason rather than as a server error.
    """
    result = client.complete_json(
        system=SYSTEM_PROMPT,
        user=build_user_message(question, entities, period),
        schema_name=SCHEMA_NAME,
        schema=ROUTE_SCHEMA,
        max_tokens=256,
    )
    data = result.data
    intent = _clean_intent(data.get("intent"))
    slots = Slots(
        entities=entities,
        period=period,
        compare_to=compare_to if intent == "Q3" else None,
        metric=_clean_choice(data.get("metric"), METRICS, "value"),
        dimension=_clean_dimension(intent, data.get("dimension")),
        direction=_clean_choice(data.get("direction"), DIRECTIONS, "largest"),
        top_n=_clean_top_n(data.get("top_n")),
    )
    # Confidence is recorded, not acted on. Gating execution on it would trade a grounded
    # answer the model happened to feel unsure about for a refusal, and the numeric verifier
    # already catches the failure that matters — prose no evidence row supports.
    routed = Route(
        intent=intent,
        slots=slots,
        premise=_clean_choice(data.get("premise"), PREMISES, "none"),
        confidence=_clean_confidence(data.get("confidence")),
        raw=data,
    )
    logger.debug("routed to %s (confidence %.2f)", routed.intent, routed.confidence)
    return routed, result
