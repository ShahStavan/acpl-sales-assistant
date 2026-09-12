"""Pydantic request and response models for the HTTP contract.

``/ask``: ``{question}`` → ``{answer, status, evidence, cost_usd, latency_ms}`` plus the
documented extra fields ``timings_ms``, ``intent`` and ``reason``. ``/actions``: ``{scope}``
→ a list of ``{finding, rule_id, action, state}`` plus ``period``, ``evidence``,
``priority``. DESIGN.md Appendix A.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------


class EvidenceRow(BaseModel):
    """One source row behind a figure, carrying the file it came from.

    Extra fields are allowed because the columns differ per source: a stock-out row
    carries ``days_out_of_stock``, a target row carries ``target_value_inr``. What every
    row shares is ``source_file``, which is what makes a figure traceable (DESIGN.md §4.6).
    """

    model_config = ConfigDict(extra="allow")

    source_file: str = Field(description="The provided file this row was read from.")


# ---------------------------------------------------------------------------
# POST /actions
# ---------------------------------------------------------------------------

ActionState = Literal["RECOMMENDED", "PENDING_APPROVAL"]


class ActionsRequest(BaseModel):
    """Request body for ``POST /actions``."""

    scope: str = Field(
        description="A region name, or 'all'. Anything else resolves to no scope and "
        "returns an empty list.",
    )


class ActionItem(BaseModel):
    """One playbook rule matched against one entity over a stated period.

    ``finding``, ``rule_id``, ``action`` and ``state`` are the contract; ``period``,
    ``evidence`` and ``priority`` are the documented extra fields (DESIGN.md §5.4).
    """

    finding: str = Field(description="What was observed, in the figures that triggered the rule.")
    rule_id: str = Field(description="The playbook rule that fired, e.g. 'R-01'.")
    action: str = Field(description="The playbook's own prescribed action, verbatim.")
    state: ActionState = Field(
        description="PENDING_APPROVAL where the playbook's needs_approval column says Yes. "
        "Nothing is executed in either state.",
    )
    period: str = Field(description="The period the finding covers, e.g. '2026-04..2026-06'.")
    evidence: list[EvidenceRow] = Field(
        default_factory=list,
        description="The source rows the finding was computed from.",
    )
    priority: int = Field(
        ge=1,
        description="1-based rank over the returned list, by recency then value at risk.",
    )


# ---------------------------------------------------------------------------
# POST /ask
# ---------------------------------------------------------------------------

AskStatus = Literal["OK", "NO_ANSWER"]


class AskRequest(BaseModel):
    """Request body for ``POST /ask``."""

    question: str = Field(description="A single natural-language question about FY26.")


class AskResponse(BaseModel):
    """Response body for ``POST /ask``.

    ``reason`` is a documented extra field, and the one the evaluation set asserts on. The
    ``answer`` of a refusal is a sentence written for a reader, so scoring against it would
    be scoring prose; the reason is a stable token — ``unknown_entity``, ``no_route``,
    ``ungrounded_figure`` — that says which refusal class fired. It is ``null`` on ``OK``.
    """

    answer: str
    status: AskStatus
    reason: str | None = Field(
        default=None,
        description="Refusal class when status is NO_ANSWER; null when the question was answered.",
    )
    evidence: list[EvidenceRow] = Field(default_factory=list)
    cost_usd: float = Field(ge=0.0)
    latency_ms: float = Field(ge=0.0)
    timings_ms: dict[str, float] = Field(default_factory=dict)
    intent: str | None = None
    models: list[str] = Field(
        default_factory=list,
        description="Provider models that served this request's LLM calls, in call order. "
        "An entry other than the configured primary means the call fell back.",
    )


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    """Liveness and readiness: the process, the warehouse it reads, the model it is set to use."""

    status: Literal["ok", "degraded"]
    warehouse: str
    model: str
    fallback_models: list[str] = Field(
        default_factory=list,
        description="Models tried after the primary when it is unavailable, in order.",
    )
    degraded_models: list[str] = Field(
        default_factory=list,
        description="Models whose circuit breaker is open, so calls are currently "
        "skipping them. Empty on a healthy process.",
    )
    detail: str | None = None


def evidence_from_rows(rows: list[dict[str, Any]]) -> list[EvidenceRow]:
    """Build evidence models from plain row dicts, each of which must name its source file."""
    return [EvidenceRow(**row) for row in rows]
