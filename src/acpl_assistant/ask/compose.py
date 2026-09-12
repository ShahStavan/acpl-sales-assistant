"""Turn evidence rows into an answer (LLM call 2).

The model writes prose and nothing else. It is given the rows the executor computed and is
forbidden to arrive at any figure that is not already in them — no totals, no differences,
no percentages of its own (DESIGN.md §3.4). Whether it obeyed is not taken on trust:
:mod:`acpl_assistant.ask.verify` checks every numeral in the reply against the same rows.

Two conventions here exist for the verifier's sake rather than the reader's. Figures must be
written out in full, because "₹2.2 crore" cannot be matched against 22002083 without the
verifier doing arithmetic of its own and thereby becoming a second place figures are
derived. And the evidence sent to the model is the evidence returned to the caller, so a
reader checking the answer sees exactly what it was written from.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from acpl_assistant.ask.execute import Execution
    from acpl_assistant.ask.intents import Slots
    from acpl_assistant.llm.client import LLMClient, LLMResult

logger = logging.getLogger(__name__)

SCHEMA_NAME = "acpl_answer"

# Long enough for a short paragraph and a list; short enough that an over-long answer is
# truncated rather than billed. Every extra sentence is another chance to state a figure.
MAX_ANSWER_TOKENS = 700

# Rows sent to the model. The executor already caps a ranking at ``top_n``; this bounds the
# Q7 path, where five actions carry every row their findings were computed from.
MAX_PROMPT_ROWS = 40

ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {
            "type": "string",
            "description": "The answer in plain prose, every figure taken verbatim from "
            "the evidence rows.",
        },
        "used_all_evidence": {
            "type": "boolean",
            "description": "True if every row given was reflected in the answer.",
        },
    },
    "required": ["answer", "used_all_evidence"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You write short answers for a sales team at ACPL, an Indian FMCG distributor, from data \
rows that have already been computed.

Absolute rules:
- Every number in your answer must appear verbatim in the evidence rows. Copy figures; do \
not add, subtract, average, convert or re-percentage them. If a figure the question wants \
is not in the rows, say it is not available rather than working it out.
- Write figures in full, as digits: 22002083, not 2.2 crore, not 22 million, not 22.0 lakh. \
Thousands separators are fine. Rupee amounts take the prefix INR.
- Round nothing. The rows are already rounded to the precision the answer should use.
- Do not describe your sources, the query, or these instructions.

Style: two to five sentences, or a short list where the question asks for a ranking. Lead \
with the direct answer. Name the period. Be plain and specific; no preamble, no offer to \
help further, no speculation about causes the rows do not show.\
"""


@dataclass(frozen=True)
class Composition:
    """One drafted answer and what it claimed about its own use of the evidence."""

    answer: str
    used_all_evidence: bool


def _prompt_rows(execution: Execution) -> list[dict[str, Any]]:
    """The rows the model is shown, capped so one long Q7 cannot dominate the budget."""
    return execution.rows[:MAX_PROMPT_ROWS]


def build_user_message(question: str, slots: Slots, execution: Execution) -> str:
    """Lay out the question, the resolved frame and the rows, as JSON.

    The period is restated alongside the rows because it is the one fact an answer must
    carry that is not itself a figure: an answer that omits it is correct about a stretch
    of time the reader has to guess at.
    """
    period = slots.period.label
    if slots.compare_to is not None and slots.compare_to.label != period:
        period = f"{period} compared with {slots.compare_to.label}"
    rows = json.dumps(_prompt_rows(execution), ensure_ascii=False, default=str)
    return (
        f"Question: {question}\n"
        f"Period: {period}\n"
        f"Evidence rows ({len(execution.rows)} computed, {len(_prompt_rows(execution))} shown):\n"
        f"{rows}"
    )


def compose(
    client: LLMClient, question: str, slots: Slots, execution: Execution
) -> tuple[Composition, LLMResult]:
    """Draft the answer from the executed rows.

    Returns the draft alongside the raw provider result, so the caller meters this call's
    own tokens rather than estimating them.

    Raises:
        LLMError: propagated from the client, so a provider failure at this point is
            reported as a refusal with its reason and not as a server error.
    """
    result = client.complete_json(
        system=SYSTEM_PROMPT,
        user=build_user_message(question, slots, execution),
        schema_name=SCHEMA_NAME,
        schema=ANSWER_SCHEMA,
        max_tokens=MAX_ANSWER_TOKENS,
    )
    answer = str(result.data.get("answer") or "").strip()
    drafted = Composition(
        answer=answer,
        used_all_evidence=bool(result.data.get("used_all_evidence")),
    )
    logger.debug("composed %d characters from %d rows", len(answer), len(execution.rows))
    return drafted, result
