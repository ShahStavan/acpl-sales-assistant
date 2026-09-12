"""The evaluation runner's grading, and the case file it grades against.

An eval harness that quietly mis-scores is worse than none: it publishes a number nobody
can trace. These tests pin the two judgements the runner makes — what counts as correct,
and what counts as not being the system's fault — and check the committed case file is
well formed before a run spends anything on it.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from eval import run_eval
from eval.run_eval import (
    PROVIDER_REASONS,
    QUESTIONS,
    CaseResult,
    evidence_blob,
    grade,
    load_cases,
    percentile,
    summarise,
)
import pytest
import yaml

EVIDENCE = [{"source_file": "fact_targets.csv", "brand": "Aqualite", "gap_value_inr": 1912659}]

ANSWERED = {
    "status": "OK",
    "reason": None,
    "intent": "Q1",
    "answer": "Aqualite is short by INR 1912659.",
    "evidence": EVIDENCE,
    "cost_usd": 0.0007,
    "latency_ms": 2100.0,
}


def case(**expect: Any) -> dict[str, Any]:
    """A case expecting an answer, unless the caller says otherwise."""
    base = {"status": "OK", "intent": "Q1", "evidence_contains": ["1912659"]}
    return {"id": "T-01", "category": "Q1", "question": "q?", "expect": {**base, **expect}}


class TestGradingAnswers:
    def test_a_correct_answer_passes(self) -> None:
        assert grade(case(), ANSWERED).passed

    def test_a_figure_no_row_carries_fails(self) -> None:
        result = grade(case(evidence_contains=["999"]), ANSWERED)
        assert not result.passed
        assert result.missing == ["999"]

    def test_every_expected_figure_must_appear(self) -> None:
        result = grade(case(evidence_contains=["1912659", "999", "888"]), ANSWERED)
        assert result.missing == ["999", "888"]

    def test_the_wrong_family_fails_even_with_the_right_figure(self) -> None:
        result = grade(case(intent="Q2"), ANSWERED)
        assert not result.passed
        assert "intent" in result.failure

    def test_a_case_naming_no_intent_does_not_check_one(self) -> None:
        expect = {"status": "OK", "evidence_contains": ["1912659"]}
        assert grade(
            {"id": "T", "category": "Q1", "question": "q", "expect": expect}, ANSWERED
        ).passed

    def test_a_refusal_where_an_answer_was_expected_fails(self) -> None:
        refused = {**ANSWERED, "status": "NO_ANSWER", "reason": "no_rows", "evidence": []}
        result = grade(case(), refused)
        assert not result.passed
        assert "status" in result.failure

    def test_the_prose_is_never_graded(self) -> None:
        """Two correct answers can be worded differently; neither is more correct for it."""
        assert grade(case(), {**ANSWERED, "answer": "Aqualite. 1912659."}).passed


class TestGradingRefusals:
    def test_the_expected_refusal_class_passes(self) -> None:
        body = {"status": "NO_ANSWER", "reason": "unknown_entity", "evidence": []}
        assert grade(case(status="NO_ANSWER", reason="unknown_entity"), body).passed

    def test_the_wrong_refusal_class_fails(self) -> None:
        body = {"status": "NO_ANSWER", "reason": "no_route", "evidence": []}
        result = grade(case(status="NO_ANSWER", reason="unknown_entity"), body)
        assert not result.passed
        assert "reason" in result.failure

    def test_an_answer_where_a_refusal_was_expected_fails(self) -> None:
        assert not grade(case(status="NO_ANSWER", reason="unknown_entity"), ANSWERED).passed


class TestProviderFaults:
    @pytest.mark.parametrize("reason", sorted(PROVIDER_REASONS))
    def test_a_provider_failure_is_not_scored_as_a_wrong_answer(self, reason: str) -> None:
        """A 429 says nothing about whether the question would have been routed correctly."""
        body = {"status": "NO_ANSWER", "reason": reason, "evidence": []}
        result = grade(case(), body)
        assert result.provider_fault
        assert not result.passed

    def test_a_case_that_expects_a_provider_reason_is_still_graded(self) -> None:
        body = {"status": "NO_ANSWER", "reason": "no_provider_key", "evidence": []}
        result = grade(case(status="NO_ANSWER", reason="no_provider_key"), body)
        assert not result.provider_fault
        assert result.passed


class TestSummary:
    def test_faults_leave_the_denominator(self) -> None:
        results = [
            CaseResult(id="a", category="Q1", question="", expected_status="OK", passed=True),
            CaseResult(id="b", category="Q1", question="", expected_status="OK", passed=False),
            CaseResult(
                id="c", category="Q1", question="", expected_status="OK", provider_fault=True
            ),
        ]
        summary = summarise(results)
        assert summary["cases"] == 3
        assert summary["graded"] == 2
        assert summary["accuracy"] == 0.5
        assert summary["provider_faults"] == 1

    def test_a_run_of_nothing_but_faults_reports_zero_rather_than_dividing(self) -> None:
        results = [
            CaseResult(
                id="a", category="Q1", question="", expected_status="OK", provider_fault=True
            )
        ]
        assert summarise(results)["accuracy"] == 0.0

    def test_accuracy_is_also_broken_down_by_category(self) -> None:
        results = [
            CaseResult(id="a", category="Q1", question="", expected_status="OK", passed=True),
            CaseResult(id="b", category="Q2", question="", expected_status="OK", passed=False),
        ]
        by_category = summarise(results)["by_category"]
        assert by_category["Q1"]["accuracy"] == 1.0
        assert by_category["Q2"]["accuracy"] == 0.0

    def test_the_total_cost_counts_the_faults_too(self) -> None:
        """A rate-limited call is excluded from accuracy, but it was still paid for."""
        results = [
            CaseResult(
                id="a",
                category="Q1",
                question="",
                expected_status="OK",
                provider_fault=True,
                cost_usd=0.001,
            )
        ]
        assert summarise(results)["total_cost_usd"] == 0.001

    @pytest.mark.parametrize(
        ("fraction", "expected"), [(0.0, 1.0), (0.5, 3.0), (0.95, 5.0), (1.0, 5.0)]
    )
    def test_percentiles_are_taken_by_nearest_rank(self, fraction: float, expected: float) -> None:
        assert percentile([5.0, 1.0, 3.0, 2.0, 4.0], fraction) == expected

    def test_an_empty_list_has_no_percentile_to_report(self) -> None:
        assert percentile([], 0.5) == 0.0


class TestEvidenceBlob:
    def test_non_ascii_survives_the_flattening(self) -> None:
        assert "Ahmedabad" in evidence_blob([{"territory": "Ahmedabad"}])

    def test_a_date_is_searchable_as_written(self) -> None:
        assert "2026-06-23" in evidence_blob([{"week": dt.date(2026, 6, 23)}])


class TestTheCommittedCaseFile:
    """The set itself, checked before a run spends anything on it."""

    CASES = load_cases(QUESTIONS)
    REFUSAL_REASONS = {
        "blocked_input",
        "unknown_entity",
        "out_of_period",
        "unsupported_metric",
        "no_route",
        "no_rows",
        "false_premise",
        "ungrounded_figure",
    }

    def test_every_id_is_unique(self) -> None:
        ids = [c["id"] for c in self.CASES]
        assert len(ids) == len(set(ids))

    def test_every_case_is_completely_specified(self) -> None:
        for c in self.CASES:
            assert c["question"].strip(), c["id"]
            assert c["category"], c["id"]
            assert c["notes"].strip(), c["id"]
            expect = c["expect"]
            assert expect["status"] in {"OK", "NO_ANSWER"}, c["id"]

    def test_every_answer_case_asserts_a_figure_and_a_family(self) -> None:
        for c in self.CASES:
            if c["expect"]["status"] != "OK":
                continue
            assert c["expect"]["evidence_contains"], c["id"]
            assert c["expect"]["intent"], c["id"]

    def test_every_refusal_case_names_a_class_the_pipeline_can_produce(self) -> None:
        for c in self.CASES:
            if c["expect"]["status"] != "NO_ANSWER":
                continue
            assert c["expect"]["reason"] in self.REFUSAL_REASONS, c["id"]

    def test_all_eight_families_are_covered(self) -> None:
        categories = {c["category"] for c in self.CASES}
        assert {f"Q{n}" for n in range(1, 9)} <= categories

    def test_every_refusal_class_the_set_can_reach_is_covered(self) -> None:
        reasons = {c["expect"].get("reason") for c in self.CASES}
        assert {
            "blocked_input",
            "unknown_entity",
            "out_of_period",
            "unsupported_metric",
            "false_premise",
            "no_route",
        } <= reasons

    def test_the_set_is_large_enough_to_measure_with(self) -> None:
        assert len(self.CASES) >= 50

    def test_the_file_is_valid_yaml_with_one_top_level_key(self) -> None:
        document = yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))
        assert set(document) == {"cases"}


class TestTheRun:
    """``main`` over a stubbed service: no network, no provider, no key."""

    @staticmethod
    def _stub(monkeypatch: pytest.MonkeyPatch, replies: list[dict[str, Any]]) -> list[str]:
        """Answer each question from *replies* in turn, recording what was asked."""
        asked: list[str] = []

        def fake_ask(client: Any, base_url: str, question: str) -> dict[str, Any]:
            asked.append(question)
            return replies[min(len(asked) - 1, len(replies) - 1)]

        monkeypatch.setattr(run_eval, "ask", fake_ask)
        return asked

    def test_a_dead_provider_stops_the_run_rather_than_grinding_through_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A quota measured per day does not clear part-way through a run."""
        asked = self._stub(
            monkeypatch, [{"status": "NO_ANSWER", "reason": "provider_rate_limited"}]
        )
        out = tmp_path / "result.json"
        run_eval.main(["--pace", "0", "--out", str(out), "--max-consecutive-faults", "3"])
        assert len(asked) == 3
        written = json.loads(out.read_text(encoding="utf-8"))
        assert "3 provider failures in a row" in written["abandoned"]

    def test_a_single_fault_does_not_stop_a_healthy_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        replies = [
            {"status": "NO_ANSWER", "reason": "provider_rate_limited"},
            {"status": "NO_ANSWER", "reason": "blocked_input"},
        ]
        asked = self._stub(monkeypatch, replies)
        out = tmp_path / "result.json"
        run_eval.main(["--only", "REFUSE-injection", "--pace", "0", "--out", str(out)])
        assert len(asked) == 4
        assert json.loads(out.read_text(encoding="utf-8"))["abandoned"] == ""

    def test_the_ceiling_can_be_lifted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        asked = self._stub(
            monkeypatch, [{"status": "NO_ANSWER", "reason": "provider_rate_limited"}]
        )
        out = tmp_path / "result.json"
        run_eval.main(
            ["--only", "Q8", "--pace", "0", "--out", str(out), "--max-consecutive-faults", "0"]
        )
        assert len(asked) == 3

    def test_a_filter_matching_nothing_is_an_error_not_an_empty_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(SystemExit):
            run_eval.main(["--only", "nonesuch", "--pace", "0"])

    def test_a_run_with_no_faults_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._stub(monkeypatch, [{"status": "NO_ANSWER", "reason": "blocked_input"}])
        out = tmp_path / "result.json"
        code = run_eval.main(["--only", "REFUSE-injection", "--pace", "0", "--out", str(out)])
        assert code == 0

    def test_an_empty_case_file_is_refused(self, tmp_path: Path) -> None:
        empty = tmp_path / "none.yaml"
        empty.write_text("cases: []", encoding="utf-8")
        with pytest.raises(SystemExit):
            load_cases(empty)
