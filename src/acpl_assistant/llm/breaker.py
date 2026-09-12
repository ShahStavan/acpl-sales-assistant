"""Per-model circuit breaker guarding the provider chain.

A model that has just refused several calls in a row will almost certainly refuse the next
one: a free-tier key that has spent its daily allowance for ``gemini-2.5-flash`` has spent
it for the rest of the day, and no backoff inside a request will clear that. Sending the
call anyway costs a round-trip and a retry delay to re-learn something already known, so
the breaker records the fault, opens for a cooldown, and lets the client skip straight to
the next model in the chain. DESIGN.md section 3.5.

State is per model and per process, and it is shared across threads: FastAPI runs the
synchronous handlers in a thread pool, so every read that mutates and every write is taken
under one lock.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
import threading
import time

logger = logging.getLogger(__name__)

# Two consecutive faults, not one: a single 429 can be a per-minute burst that the next
# call clears, and opening on it would route a healthy model's traffic to a weaker one for
# five minutes. Two in a row is no longer a burst.
DEFAULT_THRESHOLD = 2

# Long enough that an exhausted daily quota is probed a handful of times over an evaluation
# run rather than on every question, short enough that a five-minute provider incident does
# not outlive itself.
DEFAULT_COOLDOWN_S = 300.0


@dataclass
class _State:
    """Consecutive faults recorded for one model, and when its cooldown ends."""

    failures: int = 0
    open_until: float | None = None


class CircuitBreaker:
    """Consecutive-failure breaker over a set of model ids.

    Closed by default; ``threshold`` consecutive faults open it for ``cooldown_s``. When the
    cooldown elapses the next call is let through as a probe — success closes the breaker,
    another fault re-opens it immediately rather than granting a fresh run of attempts.

    A cooldown of zero disables the breaker: faults are still counted, nothing is ever
    skipped. That is the escape hatch for an operator who wants the chain tried in full on
    every call.
    """

    def __init__(
        self,
        *,
        threshold: int = DEFAULT_THRESHOLD,
        cooldown_s: float = DEFAULT_COOLDOWN_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = max(1, int(threshold))
        self._cooldown_s = max(0.0, float(cooldown_s))
        # Monotonic by default, for the same reason the meter uses it: a wall clock that
        # steps backwards over an NTP correction would hold a breaker open indefinitely.
        self._clock = clock
        self._lock = threading.Lock()
        self._states: dict[str, _State] = {}

    @property
    def threshold(self) -> int:
        """Consecutive faults that open a model's breaker."""
        return self._threshold

    @property
    def cooldown_s(self) -> float:
        """Seconds a model is skipped for once its breaker opens; ``0`` disables skipping."""
        return self._cooldown_s

    def is_open(self, model: str) -> bool:
        """Whether *model* should be skipped right now.

        Not a pure predicate: a model whose cooldown has just elapsed is moved to half-open
        here and reported as usable, which is what lets the next call probe it.
        """
        with self._lock:
            state = self._states.get(model)
            if state is None or state.open_until is None:
                return False
            if self._clock() < state.open_until:
                return True
            state.open_until = None
            state.failures = self._threshold - 1
            logger.info("circuit half-open for model %r: probing with the next call", model)
            return False

    def record_success(self, model: str) -> None:
        """Clear *model*'s fault history: the breaker counts *consecutive* faults only."""
        with self._lock:
            state = self._states.pop(model, None)
            if state is not None and (state.failures or state.open_until):
                logger.info("circuit closed for model %r", model)

    def record_failure(self, model: str) -> None:
        """Record one fault against *model*, opening the breaker at the threshold."""
        with self._lock:
            state = self._states.setdefault(model, _State())
            state.failures += 1
            if state.failures >= self._threshold and self._cooldown_s > 0:
                state.open_until = self._clock() + self._cooldown_s
                logger.warning(
                    "circuit open for model %r for %.0fs after %d consecutive faults",
                    model,
                    self._cooldown_s,
                    state.failures,
                )

    def open_models(self) -> list[str]:
        """Models currently in cooldown, sorted — what ``GET /health`` reports as degraded."""
        now = self._clock()
        with self._lock:
            return sorted(
                model
                for model, state in self._states.items()
                if state.open_until is not None and now < state.open_until
            )

    def reset(self) -> None:
        """Forget every model's history. For tests and for an operator forcing a retry."""
        with self._lock:
            self._states.clear()
