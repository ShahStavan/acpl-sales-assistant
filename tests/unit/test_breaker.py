"""The per-model circuit breaker: opening, cooldown, the half-open probe, and closing.

Time is injected rather than slept through. A test that proved a five-minute cooldown by
waiting five minutes would not be run, and one that shortened the cooldown to prove it
would be testing a different object than the one that ships.
"""

from __future__ import annotations

from acpl_assistant.llm.breaker import DEFAULT_COOLDOWN_S, DEFAULT_THRESHOLD, CircuitBreaker

FLASH = "gemini-2.5-flash"
LITE = "gemini-2.5-flash-lite"


class Clock:
    """A monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def breaker(clock: Clock, *, threshold: int = 2, cooldown_s: float = 300.0) -> CircuitBreaker:
    return CircuitBreaker(threshold=threshold, cooldown_s=cooldown_s, clock=clock)


class TestDefaults:
    def test_an_unknown_model_is_closed(self) -> None:
        assert not CircuitBreaker().is_open(FLASH)

    def test_the_shipped_defaults_are_the_documented_ones(self) -> None:
        instance = CircuitBreaker()
        assert instance.threshold == DEFAULT_THRESHOLD
        assert instance.cooldown_s == DEFAULT_COOLDOWN_S

    def test_a_threshold_below_one_would_open_on_nothing(self) -> None:
        assert CircuitBreaker(threshold=0).threshold == 1

    def test_a_negative_cooldown_is_no_cooldown(self) -> None:
        assert CircuitBreaker(cooldown_s=-5).cooldown_s == 0.0


class TestOpening:
    def test_one_fault_below_the_threshold_leaves_it_closed(self) -> None:
        """A single 429 can be a per-minute burst; routing away from a model for that
        would cost more than the retry it saves."""
        clock = Clock()
        instance = breaker(clock)
        instance.record_failure(FLASH)
        assert not instance.is_open(FLASH)

    def test_consecutive_faults_at_the_threshold_open_it(self) -> None:
        clock = Clock()
        instance = breaker(clock)
        instance.record_failure(FLASH)
        instance.record_failure(FLASH)
        assert instance.is_open(FLASH)

    def test_faults_are_counted_consecutively_not_cumulatively(self) -> None:
        clock = Clock()
        instance = breaker(clock)
        instance.record_failure(FLASH)
        instance.record_success(FLASH)
        instance.record_failure(FLASH)
        assert not instance.is_open(FLASH), "a success in between resets the run"

    def test_one_model_opening_does_not_open_another(self) -> None:
        clock = Clock()
        instance = breaker(clock)
        instance.record_failure(FLASH)
        instance.record_failure(FLASH)
        assert instance.is_open(FLASH)
        assert not instance.is_open(LITE)


class TestCooldown:
    def test_it_stays_open_for_the_whole_cooldown(self) -> None:
        clock = Clock()
        instance = breaker(clock, cooldown_s=300.0)
        instance.record_failure(FLASH)
        instance.record_failure(FLASH)
        clock.advance(299.0)
        assert instance.is_open(FLASH)

    def test_the_next_call_after_the_cooldown_is_let_through(self) -> None:
        clock = Clock()
        instance = breaker(clock, cooldown_s=300.0)
        instance.record_failure(FLASH)
        instance.record_failure(FLASH)
        clock.advance(301.0)
        assert not instance.is_open(FLASH)

    def test_a_failed_probe_re_opens_on_one_fault(self) -> None:
        """Half-open grants one call, not a fresh run of the threshold."""
        clock = Clock()
        instance = breaker(clock, threshold=2, cooldown_s=300.0)
        instance.record_failure(FLASH)
        instance.record_failure(FLASH)
        clock.advance(301.0)
        assert not instance.is_open(FLASH)  # the probe
        instance.record_failure(FLASH)
        assert instance.is_open(FLASH)

    def test_a_successful_probe_closes_it_for_good(self) -> None:
        clock = Clock()
        instance = breaker(clock, threshold=2, cooldown_s=300.0)
        instance.record_failure(FLASH)
        instance.record_failure(FLASH)
        clock.advance(301.0)
        assert not instance.is_open(FLASH)
        instance.record_success(FLASH)
        instance.record_failure(FLASH)
        assert not instance.is_open(FLASH), "the run started again from zero"

    def test_a_zero_cooldown_never_skips_a_model(self) -> None:
        """The escape hatch: count the faults, act on none of them."""
        clock = Clock()
        instance = breaker(clock, cooldown_s=0.0)
        for _ in range(10):
            instance.record_failure(FLASH)
        assert not instance.is_open(FLASH)
        assert instance.open_models() == []


class TestReporting:
    def test_open_models_names_only_what_is_in_cooldown(self) -> None:
        clock = Clock()
        instance = breaker(clock)
        instance.record_failure(FLASH)
        assert instance.open_models() == []
        instance.record_failure(FLASH)
        assert instance.open_models() == [FLASH]

    def test_open_models_drops_a_model_whose_cooldown_has_elapsed(self) -> None:
        clock = Clock()
        instance = breaker(clock, cooldown_s=300.0)
        instance.record_failure(FLASH)
        instance.record_failure(FLASH)
        clock.advance(301.0)
        assert instance.open_models() == []

    def test_open_models_is_sorted_so_the_health_field_is_stable(self) -> None:
        clock = Clock()
        instance = breaker(clock, threshold=1)
        instance.record_failure(LITE)
        instance.record_failure(FLASH)
        assert instance.open_models() == sorted([FLASH, LITE])

    def test_a_success_on_an_untouched_model_is_a_no_op(self) -> None:
        instance = CircuitBreaker()
        instance.record_success(FLASH)
        assert instance.open_models() == []

    def test_reset_forgets_everything(self) -> None:
        clock = Clock()
        instance = breaker(clock, threshold=1)
        instance.record_failure(FLASH)
        assert instance.is_open(FLASH)
        instance.reset()
        assert not instance.is_open(FLASH)
