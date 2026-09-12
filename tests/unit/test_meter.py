"""Pricing and the request meter: the two places a reported figure could be invented."""

from __future__ import annotations

import logging

import pytest

from acpl_assistant.llm import pricing
from acpl_assistant.llm.client import LLMResult, Usage
from acpl_assistant.llm.pricing import PER_MILLION, RATES, cost_usd, rate_for
from acpl_assistant.obs.meter import Meter


def result(model: str = "gemini-2.5-flash", prompt: int = 1000, completion: int = 100) -> LLMResult:
    """One provider call's worth of usage, priced the way the client prices it."""
    usage = Usage(
        prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion
    )
    return LLMResult(
        data={},
        usage=usage,
        model=model,
        cost_usd=cost_usd(model, usage.prompt_tokens, usage.billable_output_tokens),
    )


class TestPricing:
    def test_every_card_is_priced_above_zero(self) -> None:
        for model, card in RATES.items():
            assert card.input_usd_per_mtok > 0, model
            assert card.output_usd_per_mtok > 0, model

    def test_output_is_never_cheaper_than_input(self) -> None:
        """A card with the two swapped would under-report the calls that cost most."""
        for model, card in RATES.items():
            assert card.output_usd_per_mtok >= card.input_usd_per_mtok, model

    def test_cost_is_the_published_rate_times_the_tokens(self) -> None:
        card = RATES["gemini-2.5-flash"]
        expected = (1000 * card.input_usd_per_mtok + 200 * card.output_usd_per_mtok) / PER_MILLION
        assert cost_usd("gemini-2.5-flash", 1000, 200) == pytest.approx(expected)

    def test_zero_tokens_cost_nothing(self) -> None:
        assert cost_usd("gemini-2.5-flash", 0, 0) == 0.0

    def test_an_unpriced_model_reports_zero_and_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Zero against non-zero usage is visible; a guessed rate would not be."""
        pricing._WARNED.discard("nonesuch-1")
        with caplog.at_level(logging.WARNING):
            assert cost_usd("nonesuch-1", 1000, 1000) == 0.0
        assert "nonesuch-1" in caplog.text

    def test_the_warning_is_logged_once_per_model(self, caplog: pytest.LogCaptureFixture) -> None:
        pricing._WARNED.discard("nonesuch-2")
        with caplog.at_level(logging.WARNING):
            cost_usd("nonesuch-2", 10, 10)
            cost_usd("nonesuch-2", 10, 10)
        assert caplog.text.count("nonesuch-2") == 1

    def test_an_unknown_model_has_no_card(self) -> None:
        assert rate_for("nonesuch-3") is None


class TestBillableOutput:
    def test_itemised_completion_tokens_are_used_as_they_stand(self) -> None:
        assert Usage(100, 40, 140).billable_output_tokens == 40

    def test_unitemised_reasoning_tokens_are_still_billed(self) -> None:
        """Gemini reports thinking tokens only inside ``total_tokens``, and bills them."""
        assert Usage(100, 14, 220).billable_output_tokens == 120

    def test_a_missing_total_does_not_produce_a_negative(self) -> None:
        assert Usage(100, 40, 0).billable_output_tokens == 40


class TestMeter:
    def test_a_stage_records_the_time_inside_it(self) -> None:
        meter = Meter()
        with meter.stage("route"):
            pass
        assert meter.timings_ms["route"] >= 0.0

    def test_a_stage_entered_twice_accumulates(self) -> None:
        """A retried provider call must report the time the caller actually waited."""
        meter = Meter()
        with meter.stage("route"):
            pass
        first = meter.timings_ms["route"]
        with meter.stage("route"):
            pass
        assert meter.timings_ms["route"] >= first

    def test_a_stage_that_raises_is_still_timed(self) -> None:
        meter = Meter()
        with pytest.raises(RuntimeError), meter.stage("route"):
            raise RuntimeError("provider down")
        assert "route" in meter.timings_ms

    def test_finish_is_idempotent(self) -> None:
        meter = Meter()
        first = meter.finish()
        assert meter.finish() == first
        assert meter.latency_ms == first

    def test_latency_runs_until_finish_is_called(self) -> None:
        meter = Meter()
        assert meter.latency_ms >= 0.0
        meter.finish()

    def test_recording_a_call_adds_its_tokens_and_cost(self) -> None:
        meter = Meter()
        meter.record(result())
        assert meter.calls == 1
        assert meter.prompt_tokens == 1000
        assert meter.completion_tokens == 100
        assert meter.cost_usd > 0

    def test_two_calls_sum_rather_than_replace(self) -> None:
        meter = Meter()
        meter.record(result())
        one = meter.cost_usd
        meter.record(result())
        assert meter.cost_usd == pytest.approx(2 * one)
        assert meter.calls == 2

    def test_the_snapshot_carries_every_metered_figure(self) -> None:
        meter = Meter()
        meter.record(result())
        meter.finish()
        snapshot = meter.snapshot()
        assert set(snapshot) == {
            "cost_usd",
            "latency_ms",
            "llm_calls",
            "prompt_tokens",
            "completion_tokens",
            "models",
        }
        assert snapshot["llm_calls"] == 1

    def test_each_call_records_the_model_that_served_it(self) -> None:
        """A request whose second call fell back is a request that says so."""
        meter = Meter()
        meter.record(result(model="gemini-2.5-flash"))
        meter.record(result(model="gemini-2.5-flash-lite"))
        assert meter.models == ["gemini-2.5-flash", "gemini-2.5-flash-lite"]
        assert meter.snapshot()["models"] == ["gemini-2.5-flash", "gemini-2.5-flash-lite"]

    def test_the_snapshot_copies_the_model_list(self) -> None:
        meter = Meter()
        meter.record(result())
        snapshot = meter.snapshot()
        meter.record(result(model="gemini-3.1-flash-lite"))
        assert snapshot["models"] == ["gemini-2.5-flash"]
