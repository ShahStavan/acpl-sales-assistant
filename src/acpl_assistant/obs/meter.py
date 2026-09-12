"""Request-scoped meter for cost and timing.

Accumulates provider token usage into ``cost_usd`` and stage timings into ``timings_ms``;
``latency_ms`` spans the whole handler via ``perf_counter_ns``. DESIGN.md §6.1–§6.2.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from acpl_assistant.llm.client import LLMResult

NS_PER_MS = 1_000_000


def _now_ns() -> int:
    """Monotonic clock reading in nanoseconds.

    ``perf_counter_ns`` rather than ``time.time``: the wall clock can step backwards over
    an NTP correction and hand the caller a negative latency.
    """
    return time.perf_counter_ns()


class Meter:
    """Cost and latency for one request, measured rather than estimated.

    Created at the top of the handler and finished at the bottom, so ``latency_ms`` is what
    the caller waited — provider round-trip, SQL and verification included — not the sum of
    the stages that happened to be instrumented.
    """

    def __init__(self) -> None:
        self._started_ns = _now_ns()
        self._finished_ns: int | None = None
        self.timings_ms: dict[str, float] = {}
        self.cost_usd: float = 0.0
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0
        self.calls: int = 0
        # One entry per provider call, in call order. A request whose router ran on the
        # primary and whose composer fell back to a second model shows both, because the
        # answer was produced by both and the reader is owed that.
        self.models: list[str] = []

    # -- timing ------------------------------------------------------------

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Record the wall time spent inside the block as stage *name*.

        A stage entered twice accumulates rather than overwrites, so a retried provider
        call reports the time the caller actually spent waiting for it.
        """
        started = _now_ns()
        try:
            yield
        finally:
            elapsed = (_now_ns() - started) / NS_PER_MS
            self.timings_ms[name] = round(self.timings_ms.get(name, 0.0) + elapsed, 3)

    def finish(self) -> float:
        """Stop the request clock and return the total latency in milliseconds.

        Idempotent: the first call fixes the figure, so a handler that finishes in a
        ``finally`` and again on the way out reports one latency rather than two.
        """
        if self._finished_ns is None:
            self._finished_ns = _now_ns()
        return self.latency_ms

    @property
    def latency_ms(self) -> float:
        """Elapsed milliseconds, frozen once :meth:`finish` has been called."""
        end = self._finished_ns if self._finished_ns is not None else _now_ns()
        return round((end - self._started_ns) / NS_PER_MS, 3)

    # -- cost --------------------------------------------------------------

    def record(self, result: LLMResult) -> None:
        """Add one provider call's usage and cost to the request totals.

        The figures come from the provider's own ``usage`` block via
        :class:`~acpl_assistant.llm.client.LLMResult`; nothing here counts tokens itself.
        """
        self.calls += 1
        self.models.append(result.model)
        self.prompt_tokens += result.usage.prompt_tokens
        self.completion_tokens += result.usage.billable_output_tokens
        self.cost_usd = round(self.cost_usd + result.cost_usd, 10)

    def snapshot(self) -> dict[str, Any]:
        """Return the metered figures as a flat dict, for the request log line."""
        return {
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
            "llm_calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "models": list(self.models),
        }
