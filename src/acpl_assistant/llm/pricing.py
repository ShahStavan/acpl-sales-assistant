"""Per-model list-price rate table (USD per million tokens) and the cost function.

``cost_usd`` is the list-price equivalent of tokens consumed, summed over a request's calls.
DESIGN.md §6.1.

Rates are transcribed from the providers' own published price pages, with the date each was
read. They are committed rather than fetched: a figure this repository publishes must not
change because a web page did, and a rate that moves should arrive as a reviewable diff.

* Gemini — https://ai.google.dev/gemini-api/docs/pricing (read 2026-09-12), paid-tier
  standard text rates.
* OpenAI — https://openai.com/api/pricing (read 2026-09-12).
"""

from __future__ import annotations

from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)

# One million, the unit every published rate is quoted in.
PER_MILLION = 1_000_000


@dataclass(frozen=True)
class RateCard:
    """Published list price for one model, in USD per million tokens."""

    input_usd_per_mtok: float
    output_usd_per_mtok: float


# Keyed by the exact model id sent to the provider, so a request cannot be priced against
# a neighbouring model's card.
RATES: dict[str, RateCard] = {
    "gemini-2.5-flash": RateCard(0.30, 2.50),
    "gemini-2.5-flash-lite": RateCard(0.10, 0.40),
    "gemini-2.5-pro": RateCard(1.25, 10.00),
    "gpt-4o-mini": RateCard(0.15, 0.60),
    "gpt-4o": RateCard(2.50, 10.00),
}

# Models already reported as unpriced, so the warning is logged once rather than per call.
_WARNED: set[str] = set()


def rate_for(model: str) -> RateCard | None:
    """Return the rate card for *model*, or ``None`` when the model is not in the table."""
    return RATES.get(model)


def cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Price one provider call from the token counts it actually reported.

    An unpriced model yields ``0.0`` and one warning, never a guess: reporting an invented
    rate would put a number in ``cost_usd`` that no price list backs, which is the failure
    this whole module exists to avoid. The token counts are still metered and logged, so a
    missing rate is visible as zero cost against non-zero usage rather than as silence.
    """
    card = rate_for(model)
    if card is None:
        if model not in _WARNED:
            _WARNED.add(model)
            logger.warning(
                "no rate card for model %r; cost_usd will report 0.0 for its calls", model
            )
        return 0.0
    return (
        prompt_tokens * card.input_usd_per_mtok + completion_tokens * card.output_usd_per_mtok
    ) / PER_MILLION
