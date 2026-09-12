"""The ``/ask`` pipeline: guard, resolve, route, execute, compose, verify.

One pass over DESIGN.md §3.3, with every stage timed and every provider call metered. The
two model calls sit in the middle of five code stages, and each of the five can end the
request on its own — which is the point. A refusal decided before the router costs nothing,
and a figure the verifier cannot ground is withheld after the answer has been written.

This module owns the refusal table. Whatever ends a request, it ends the same way: status
``NO_ANSWER``, a stable reason token the evaluation set asserts on, and a sentence saying
what could not be done. Nothing here raises; a provider that is down is one more reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Any

from acpl_assistant.ask import compose as compose_stage, guard, verify as verify_stage
from acpl_assistant.ask.execute import execute
from acpl_assistant.ask.resolve import Refusal, parse_periods, resolve
from acpl_assistant.ask.router import route
from acpl_assistant.llm.client import LLMError
from acpl_assistant.obs.meter import Meter

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

    from acpl_assistant.ask.resolve import Vocabulary
    from acpl_assistant.llm.client import LLMClient

logger = logging.getLogger(__name__)

STATUS_OK = "OK"
STATUS_NO_ANSWER = "NO_ANSWER"

# Stage names, fixed here because they are published in ``timings_ms`` and a renamed stage
# would silently break whatever is charting them.
STAGE_GUARD = "guard"
STAGE_RESOLVE = "resolve"
STAGE_ROUTE = "route"
STAGE_EXECUTE = "execute"
STAGE_COMPOSE = "compose"
STAGE_VERIFY = "verify"

# A draft that survives verification but says nothing is not an answer; the provider
# returned an empty string and the caller is owed the reason rather than the blank.
EMPTY_ANSWER = "empty_answer"

PROVIDER_MESSAGES: dict[str, str] = {
    "no_provider_key": (
        "No language-model key is configured, so this question cannot be answered. "
        "Set LLM_API_KEY and retry; /actions and /health do not need one."
    ),
    "provider_timeout": (
        "The language model did not respond in time, so no answer was composed. "
        "The figures behind this question are unaffected — retry."
    ),
    "provider_rate_limited": (
        "The language model is rate-limiting this key, so no answer was composed. Retry shortly."
    ),
    "provider_error": (
        "The language model could not be reached, so no answer was composed. No figure is "
        "reported rather than one produced without it."
    ),
}


@dataclass(frozen=True)
class AskOutcome:
    """Everything ``POST /ask`` returns, with the metered figures it was produced under."""

    answer: str
    status: str
    reason: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    intent: str | None = None
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    timings_ms: dict[str, float] = field(default_factory=dict)
    models: list[str] = field(default_factory=list)

    @property
    def answered(self) -> bool:
        """Whether a figure was actually reported."""
        return self.status == STATUS_OK


def _provider_message(reason: str) -> str:
    """The sentence a provider failure is reported with."""
    return PROVIDER_MESSAGES.get(reason, PROVIDER_MESSAGES["provider_error"])


def _refused(
    refusal: Refusal, meter: Meter, intent: str | None = None, evidence: list | None = None
) -> AskOutcome:
    """Close the request on a refusal, carrying the meter's figures as they stand."""
    meter.finish()
    return AskOutcome(
        answer=refusal.message,
        status=STATUS_NO_ANSWER,
        reason=refusal.reason,
        evidence=evidence if evidence is not None else refusal.evidence,
        intent=intent,
        cost_usd=meter.cost_usd,
        latency_ms=meter.latency_ms,
        timings_ms=dict(meter.timings_ms),
        models=list(meter.models),
    )


def answer_question(
    con: DuckDBPyConnection,
    client: LLMClient,
    vocabulary: Vocabulary,
    question: str,
    meter: Meter | None = None,
) -> AskOutcome:
    """Answer one question, or say why it was not answered.

    The connection is the service's shared read-only handle and the client its shared
    provider connection; both are owned by the caller for the life of the process. The
    vocabulary was read from the same warehouse at startup.
    """
    meter = meter or Meter()

    # --- guard: talking to the system rather than about the data ----------
    with meter.stage(STAGE_GUARD):
        verdict = guard.screen(question)
    if verdict.blocked:
        return _refused(Refusal(reason=guard.BLOCKED_REASON, message=verdict.message), meter)

    # --- resolve: entities and period, from the warehouse's own vocabularies
    with meter.stage(STAGE_RESOLVE):
        resolution = resolve(question, vocabulary)
        periods = parse_periods(question)
    if resolution.refusal is not None:
        return _refused(resolution.refusal, meter)
    compare_to = periods[1] if len(periods) > 1 else None

    # --- route: the first and smaller of the two model calls --------------
    try:
        with meter.stage(STAGE_ROUTE):
            routed, routing_call = route(
                client, question, resolution.entities, resolution.period, compare_to
            )
    except LLMError as exc:
        logger.warning("router call failed: %s", exc.reason)
        return _refused(Refusal(reason=exc.reason, message=_provider_message(exc.reason)), meter)
    meter.record(routing_call)

    # --- execute: every figure in the response originates here ------------
    with meter.stage(STAGE_EXECUTE):
        executed = execute(con, routed.intent, routed.slots)
    if executed.refusal is not None:
        return _refused(executed.refusal, meter, intent=executed.intent)

    # --- compose: prose from the rows, and only from the rows -------------
    try:
        with meter.stage(STAGE_COMPOSE):
            drafted, compose_call = compose_stage.compose(client, question, routed.slots, executed)
    except LLMError as exc:
        logger.warning("compose call failed: %s", exc.reason)
        return _refused(
            Refusal(reason=exc.reason, message=_provider_message(exc.reason)),
            meter,
            intent=executed.intent,
            evidence=executed.rows,
        )
    meter.record(compose_call)

    # --- verify: premise, then every numeral against the rows -------------
    with meter.stage(STAGE_VERIFY):
        if not drafted.answer:
            checked = verify_stage.Verdict(
                ok=False,
                reason=EMPTY_ANSWER,
                message=(
                    "The language model returned no text, so nothing was reported. The "
                    "figures behind this question are unaffected — retry."
                ),
            )
        else:
            checked = verify_stage.verify(
                drafted.answer, executed.rows, routed.slots, routed.premise
            )
    if not checked.ok:
        logger.warning("answer withheld: %s", checked.reason)
        return _refused(
            Refusal(reason=checked.reason, message=checked.message),
            meter,
            intent=executed.intent,
            # The evidence still travels: a withheld answer is more useful with the rows it
            # should have been written from than without them.
            evidence=executed.rows,
        )

    meter.finish()
    return AskOutcome(
        answer=drafted.answer,
        status=STATUS_OK,
        reason=None,
        evidence=executed.rows,
        intent=executed.intent,
        cost_usd=meter.cost_usd,
        latency_ms=meter.latency_ms,
        timings_ms=dict(meter.timings_ms),
        models=list(meter.models),
    )
