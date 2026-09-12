"""Evaluation runner: posts every case in ``questions.yaml`` to a running service.

Reports accuracy, median ``cost_usd`` and p50/p95 ``latency_ms`` and writes a timestamped
JSON result under ``eval/results/``. DESIGN.md §6.3.

Three things this runner deliberately does not do. It does not read figures out of the
prose — the answer's wording is not graded, only the evidence behind it, because two
correct answers can be worded differently and neither is more correct for it. It does not
count a provider outage as a wrong answer: a 429 says nothing about whether the system
would have routed the question correctly, so those cases are reported separately and
excluded from the denominator. And it does not start the service — the thing measured is
the HTTP surface a reviewer would call, not an in-process shortcut around it.

Usage::

    python eval/run_eval.py --base-url http://127.0.0.1:8000
    python eval/run_eval.py --only Q1 --pace 0

A full run needs roughly twice as many provider requests as there are cases, which is more
than Gemini's free daily allowance for gemini-2.5-flash. Point ``LLM_MODEL`` at a model with
a larger allowance, use a paid key, or set ``LLM_FALLBACK_MODELS`` so the service steps down
to a model that still has allowance when the primary runs out. A run that fell back is still
a valid measurement, but it measures a different system: the summary therefore counts the
calls each model served, and the published figure should name them.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass, field
import datetime as dt
import json
from pathlib import Path
import statistics
import sys
import time
from typing import Any

import httpx
import yaml

HERE = Path(__file__).resolve().parent
QUESTIONS = HERE / "questions.yaml"
RESULTS = HERE / "results"

DEFAULT_BASE_URL = "http://127.0.0.1:8000"

# One question costs two provider calls, and the free tier throttles per minute as well as
# capping per day, so twelve seconds between questions keeps the per-minute limit out of the
# way. It does nothing for the daily cap, which pacing cannot solve: 20 requests a day
# against gemini-2.5-flash will not run this set at any speed. Set --pace 0 against a paid
# key, a model with a larger free allowance, or a local stub.
DEFAULT_PACE_S = 12.0

# Per-request timeout. Generous, because a retried rate limit inside the client can add
# thirty seconds to a call that is going to succeed.
DEFAULT_TIMEOUT_S = 120.0

# Refusals that say something about the provider rather than about the system's judgement.
PROVIDER_REASONS = frozenset(
    {"no_provider_key", "provider_timeout", "provider_rate_limited", "provider_error"}
)

# Stop after this many provider failures in a row. The free tier's cap is measured per
# *day*, so a run that has been refused three times running will be refused sixty times
# running; the useful response is to say so and stop, not to spend an hour proving it.
MAX_CONSECUTIVE_FAULTS = 3

STATUS_OK = "OK"


@dataclass
class CaseResult:
    """One case, what it expected, and what the service actually did."""

    id: str
    category: str
    question: str
    expected_status: str
    expected_reason: str = ""
    expected_intent: str = ""
    status: str = ""
    reason: str | None = None
    intent: str | None = None
    answer: str = ""
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    evidence_rows: int = 0
    models: list[str] = field(default_factory=list)
    """Provider models that served this case's calls, in call order."""

    passed: bool = False
    provider_fault: bool = False
    missing: list[str] = field(default_factory=list)
    """Expected evidence values that no returned row carried."""

    failure: str = ""
    """Why this case failed, in one phrase; empty when it passed."""


def load_cases(path: Path) -> list[dict[str, Any]]:
    """Read the labelled cases, failing loudly on an empty or malformed file."""
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cases = document.get("cases") or []
    if not cases:
        raise SystemExit(f"no cases in {path}")
    return cases


def evidence_blob(evidence: list[dict[str, Any]]) -> str:
    """Flatten the evidence to one searchable string.

    Substring matching over the serialised rows, rather than a per-column comparison: the
    column an expected figure lands in is the executor's business, and a case that names
    the column would fail on a rename that changed no figure.
    """
    return json.dumps(evidence, ensure_ascii=False, default=str)


def grade(case: dict[str, Any], body: dict[str, Any]) -> CaseResult:
    """Score one response against its case."""
    expect = case.get("expect") or {}
    result = CaseResult(
        id=case["id"],
        category=case.get("category", ""),
        question=case["question"],
        expected_status=expect.get("status", STATUS_OK),
        expected_reason=expect.get("reason", "") or "",
        expected_intent=expect.get("intent", "") or "",
        status=str(body.get("status", "")),
        reason=body.get("reason"),
        intent=body.get("intent"),
        answer=str(body.get("answer", "")),
        cost_usd=float(body.get("cost_usd", 0.0)),
        latency_ms=float(body.get("latency_ms", 0.0)),
        evidence_rows=len(body.get("evidence") or []),
        models=[str(m) for m in body.get("models") or []],
    )

    # A provider failure the case did not ask for is not evidence about this system.
    if result.reason in PROVIDER_REASONS and result.reason != result.expected_reason:
        result.provider_fault = True
        result.failure = f"provider: {result.reason}"
        return result

    if result.status != result.expected_status:
        result.failure = f"status {result.status}, expected {result.expected_status}"
        return result

    if result.expected_status != STATUS_OK:
        if result.reason != result.expected_reason:
            result.failure = f"reason {result.reason}, expected {result.expected_reason}"
            return result
        result.passed = True
        return result

    if result.expected_intent and result.intent != result.expected_intent:
        result.failure = f"intent {result.intent}, expected {result.expected_intent}"
        return result

    blob = evidence_blob(body.get("evidence") or [])
    result.missing = [
        str(value) for value in expect.get("evidence_contains", ()) if str(value) not in blob
    ]
    if result.missing:
        result.failure = f"evidence missing {', '.join(result.missing)}"
        return result

    result.passed = True
    return result


def ask(client: httpx.Client, base_url: str, question: str) -> dict[str, Any]:
    """Post one question, turning a transport failure into a provider-fault body.

    The runner must survive a service that falls over mid-run: the cases already measured
    are worth keeping, and a run that crashes at case forty reports nothing at all.
    """
    try:
        response = client.post(f"{base_url}/ask", json={"question": question})
    except httpx.HTTPError as exc:
        return {"status": "NO_ANSWER", "reason": "provider_error", "answer": repr(exc)}
    if response.status_code != httpx.codes.OK:
        return {
            "status": "NO_ANSWER",
            "reason": "provider_error",
            "answer": f"HTTP {response.status_code}",
        }
    return response.json()


def percentile(values: list[float], fraction: float) -> float:
    """The value at *fraction* through the sorted list, by nearest rank."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def summarise(results: list[CaseResult]) -> dict[str, Any]:
    """Accuracy, cost and latency over the cases that actually reached a verdict."""
    graded = [r for r in results if not r.provider_fault]
    served = Counter(model for r in results for model in r.models)
    passed = [r for r in graded if r.passed]
    latencies = [r.latency_ms for r in graded]
    costs = [r.cost_usd for r in graded]
    by_category: dict[str, dict[str, int]] = {}
    for result in graded:
        bucket = by_category.setdefault(result.category, {"passed": 0, "total": 0})
        bucket["total"] += 1
        bucket["passed"] += int(result.passed)
    return {
        "cases": len(results),
        "graded": len(graded),
        "passed": len(passed),
        "provider_faults": len(results) - len(graded),
        "accuracy": round(len(passed) / len(graded), 4) if graded else 0.0,
        "median_cost_usd": round(statistics.median(costs), 6) if costs else 0.0,
        "total_cost_usd": round(sum(r.cost_usd for r in results), 6),
        "latency_p50_ms": round(percentile(latencies, 0.50), 1),
        "latency_p95_ms": round(percentile(latencies, 0.95), 1),
        # Provider calls per model. More than one entry means the run degraded part-way
        # through, and the accuracy above is not a figure about a single model.
        "calls_by_model": dict(served.most_common()),
        "by_category": {
            name: {**counts, "accuracy": round(counts["passed"] / counts["total"], 4)}
            for name, counts in sorted(by_category.items())
        },
    }


def report(results: list[CaseResult], summary: dict[str, Any]) -> None:
    """Print the failures first, then the figures. Passing cases need no attention."""
    failures = [r for r in results if not r.passed]
    if failures:
        print("\nnot passed:")
        for result in failures:
            print(f"  {result.id:28s} {result.failure}")
    print("\n" + "-" * 64)
    print(
        f"accuracy            {summary['accuracy']:.1%}  ({summary['passed']}/{summary['graded']})"
    )
    print(f"provider faults     {summary['provider_faults']}")
    print(f"median cost/question ${summary['median_cost_usd']:.6f}")
    print(f"total cost          ${summary['total_cost_usd']:.4f}")
    print(
        f"latency p50 / p95   {summary['latency_p50_ms']:.0f} ms / {summary['latency_p95_ms']:.0f} ms"
    )
    served = summary["calls_by_model"]
    if served:
        served_text = ", ".join(f"{model} x{count}" for model, count in served.items())
        print(f"calls by model      {served_text}")
        if len(served) > 1:
            print("  (this run fell back: the accuracy above is not one model's)")
    print("\nby category:")
    for name, counts in summary["by_category"].items():
        print(f"  {name:18s} {counts['passed']:>2}/{counts['total']:<2}  {counts['accuracy']:.0%}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Command-line options for the runner."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Service to post to.")
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="Per-request timeout, seconds."
    )
    parser.add_argument(
        "--pace",
        type=float,
        default=DEFAULT_PACE_S,
        help="Seconds between questions, for a per-minute provider quota. 0 to go flat out.",
    )
    parser.add_argument(
        "--only", default="", help="Run only cases whose id or category contains this text."
    )
    parser.add_argument(
        "--max-consecutive-faults",
        type=int,
        default=MAX_CONSECUTIVE_FAULTS,
        help="Abandon the run after this many provider failures in a row. 0 to never stop.",
    )
    parser.add_argument("--questions", type=Path, default=QUESTIONS, help="Case file to run.")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Where to write the result JSON (default: timestamped).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the set and write the result. Returns a process exit code."""
    args = parse_args(argv)
    cases = load_cases(args.questions)
    if args.only:
        needle = args.only.casefold()
        cases = [
            c
            for c in cases
            if needle in c["id"].casefold() or needle in c.get("category", "").casefold()
        ]
        if not cases:
            raise SystemExit(f"no cases match {args.only!r}")

    started = dt.datetime.now(dt.UTC)
    results: list[CaseResult] = []
    consecutive = 0
    abandoned = ""
    with httpx.Client(timeout=args.timeout) as client:
        for index, case in enumerate(cases, start=1):
            if index > 1 and args.pace > 0:
                time.sleep(args.pace)
            body = ask(client, args.base_url, case["question"])
            result = grade(case, body)
            results.append(result)
            mark = "ok  " if result.passed else ("SKIP" if result.provider_fault else "FAIL")
            print(f"{mark} {index:>3}/{len(cases)} {result.id:28s} {result.failure}")

            consecutive = consecutive + 1 if result.provider_fault else 0
            if args.max_consecutive_faults and consecutive >= args.max_consecutive_faults:
                # A quota measured per day does not clear part-way through a run, and neither
                # does a service that is down. Grinding through the remaining cases would
                # spend an hour producing a file of skips.
                abandoned = f"{consecutive} provider failures in a row after {index} cases"
                print(f"\nabandoned: {abandoned}")
                break

    summary = summarise(results)
    report(results, summary)

    RESULTS.mkdir(parents=True, exist_ok=True)
    out = args.out or RESULTS / f"eval-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(
        json.dumps(
            {
                "started_utc": started.isoformat(),
                "base_url": args.base_url,
                "questions": str(args.questions.name),
                "abandoned": abandoned,
                "summary": summary,
                "cases": [asdict(r) for r in results],
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\nwritten to {out}")
    return 0 if summary["provider_faults"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
