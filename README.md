# ACPL Sales Focus & Action Assistant

Grounded answers over Aravalli Consumer Products' FY26 sales data, and weekly actions drawn
from ACPL's own action playbook.

**Governing principle: numbers are computed by code, never by a model.** The LLM classifies a
question and renders prose; every figure originates in a SQL result and every action in a
playbook rule evaluated in code. `POST /actions` calls no model at all.

| Artefact | What it is |
|---|---|
| [APPROACH.md](APPROACH.md) | Two-page design summary (sections A–F) |
| [DESIGN.md](DESIGN.md) | Detailed design, diagrams, module map |
| [ARTEFACT.md](ARTEFACT.md) · [ARTEFACT.html](ARTEFACT.html) | Self-audit in measured numbers |
| [eval/](eval/) | Labelled evaluation set, runner, committed results |

## Status

| Endpoint | State |
|---|---|
| `POST /actions` | **Serving.** All eight playbook rules, approval gating, ranked output |
| `GET /health` | **Serving.** |
| `POST /ask` | **Serving.** Guard, resolve, route, execute, compose, verify — two model calls, every figure from SQL |

## Prerequisites

- Python 3.11 or newer
- No API key is needed for data preparation or for `/actions`; the engine is code only
- `POST /ask` needs one: set `LLM_API_KEY` in `.env`. Without it every question returns
  `NO_ANSWER` with reason `no_provider_key` — never a 500, and never a figure

## Install

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"          # or: pip install -r requirements.txt
```

## Prepare the data

One command reads the provided pack, reconciles it and writes the warehouse:

```bash
python prepare.py
```

It produces `warehouse.duckdb` and `prep_report.json`, printing every reconciliation outcome
as it goes. The command is **gated**: it asserts twelve row counts and all seven
reconciliation items, and exits non-zero if any moves, so a changed input fails the build
rather than quietly shifting a figure that ARTEFACT.md has already published.

The provided data pack under `data/` is never modified — preparation only reads it, and CI
fails on any diff there.

## Run the service

```bash
python -m uvicorn acpl_assistant.service:app --host 0.0.0.0 --port 8000
# or:  make serve          # or:  acpl-serve
```

The service opens the warehouse **read-only**. It has no write path, and no outbound channel
other than the configured LLM provider (which `/actions` never uses).

Copy `.env.example` to `.env` to change the port, the warehouse path or the provider
settings. `.env` is git-ignored; never commit a key.

## Containers

```bash
docker build -t acpl-assistant .
docker run --rm -p 8000:8000 acpl-assistant
```

The image prepares the data at build time and runs as a non-root user.

---

## `POST /actions`

Every playbook rule that fires for a scope, ranked, with the figures and source rows behind
each. No model is called, so the response is reproducible from the warehouse alone.

**Request**

```json
{ "scope": "West" }
```

**Response** — a JSON array, most urgent first:

```json
[
  {
    "finding": "Aqualite in the West reached 62% of target over 2026-04..2026-06, INR 1,912,659 below plan, with its SKUs out of stock in 9 weeks there.",
    "rule_id": "R-01",
    "action": "Expedite replenishment and escalate to the regional supply lead",
    "state": "PENDING_APPROVAL",
    "period": "2026-04..2026-06",
    "evidence": [
      { "source_file": "fact_primary_sales.csv", "month": "2026-04", "brand": "Aqualite", "region": "West", "actual_value_inr": 963021.12 },
      { "source_file": "fact_targets.csv", "month": "2026-04", "brand": "Aqualite", "region": "West", "target_value_inr": 1553000 }
    ],
    "priority": 1
  }
]
```

### Fields

`finding`, `rule_id`, `action` and `state` are the contract. The rest are **extra fields**
beyond it, documented here:

| Field | Meaning |
|---|---|
| `finding` | What was observed, in the figures that triggered the rule. Templated from those figures — no model writes it, and every numeral in it also appears in `evidence` or in the period |
| `rule_id` | The playbook rule that fired, `R-01` to `R-08` |
| `action` | ACPL's own prescribed action, quoted from `action_playbook.xlsx` |
| `state` | `PENDING_APPROVAL` or `RECOMMENDED` — see below |
| `period` | The period the finding covers: `2026-02`, `2026-04..2026-06`, or ISO dates for week-grain rules (`2026-04-14..2026-06-09`) |
| `evidence` | The source rows the finding was computed from, each naming its `source_file` |
| `priority` | 1-based rank over the returned list, most recent and most at risk first |

### Scope

`scope` resolves case-insensitively to one of the four held regions, or to `all`. A trailing
"region", "india" or "zone" is tolerated, so `"west"`, `"WEST"` and `"west region"` all mean
West.

**Anything else returns `[]`.** There is deliberately no fuzzy matching: resolving `"Wets"` to
West would hand a manager the actions for a region they did not ask about, and a withheld
empty list is the safer failure. A scope with nothing to report also returns `[]` — no filler
is invented to fill a quiet week.

A scope field that is absent or not a string is a `422`, not an empty list: a malformed
request and an unrecognised region are different failures.

### Approval gating

`state` is read from the playbook's `needs_approval` column, never inferred. R-01, R-04 and
R-08 are `PENDING_APPROVAL`; the rest are `RECOMMENDED`. The escalation SOP agrees
independently — supply escalations, replenishment orders and distributor calls change a
commitment, while analysis and review do not.

**Nothing is executed in either state.** The service has no outbound channel and no write
path; it reports what should happen and stops there.

### The eight rules

| Rule | Condition as coded | Approval | Fires in FY26 |
|---|---|---|---|
| R-01 | achievement < 70% and ≥ 2 stock-out weeks on the brand's SKUs in that region | **Yes** | Aqualite / West / Apr–Jun at 62% |
| R-02 | achievement < 80%, promotion overlapping the month, uplift < 10% | No | none |
| R-03 | achievement < 80%, no stock-out, no promotion, supporting note | No | CremeDelight / North / Feb at 72% |
| R-04 | one distributor × SKU out of stock more than 6 weeks | **Yes** | D032 and D033 × BV-0104, 9 weeks each |
| R-05 | achievement > 110% | No | none — the best cell is 108.7% |
| R-06 | achievement < 80%, no stock-out, no promotion, no note | No | MintGuard / East / Mar at 74% |
| R-07 | promotion uplift > 25% | No | 23 promotions |
| R-08 | distributor with ≥ 3 distinct SKUs out in a month | **Yes** | 27 distributors |

R-02 and R-05 have no qualifying case on this data. Both stay implemented and tested; the
system reports no case rather than moving a threshold until something appears.

Findings are **grouped by the entity the action targets** — one R-01 for Aqualite in the West
citing three months, one R-08 per distributor citing its months — then ranked by recency and
rupees at risk. Nothing is capped: `{"scope": "all"}` returns the complete list of 55 actions.

Two rules carry no rupee figure. The stock-out log holds days, not value, so R-04 and R-08
rank on weeks and SKUs out rather than on an invented rupee proxy, and sort after findings
that do carry one.

## `GET /health`

```json
{
  "status": "ok",
  "warehouse": "/app/warehouse.duckdb",
  "model": "gemini-2.5-flash",
  "fallback_models": ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-3.6-flash", "gemini-3.5-flash"],
  "degraded_models": []
}
```

Returns `503` with `"status": "degraded"` when the warehouse cannot be read, so a probe can
tell "process up, data missing" from "process down". `model` and `fallback_models` are
configuration, not a reachability claim — `/actions` needs no provider at all.
`degraded_models` is observation: the models whose circuit breaker is open right now, empty
until a provider call has actually been made.

## `POST /ask`

One natural-language question about FY26, answered from SQL results, or refused with the
reason it could not be.

**Request**

```json
{ "question": "Where are we losing most against target this quarter?" }
```

**Response**

```json
{
  "answer": "Aqualite in West is the largest shortfall in FY26 Q4, at INR 1912659 against target. SparkClean in South follows at INR 390070.",
  "status": "OK",
  "reason": null,
  "intent": "Q1",
  "evidence": [
    { "source_file": "fact_primary_sales.csv + fact_targets.csv", "brand": "Aqualite", "region": "West", "period": "FY26 Q4", "actual_value_inr": 3120341, "target_value_inr": 5033000, "gap_value_inr": 1912659, "achievement_ratio": 0.62, "achievement_pct": 62 }
  ],
  "cost_usd": 0.000712,
  "latency_ms": 2841.3,
  "timings_ms": { "guard": 0.1, "resolve": 12.4, "route": 1103.8, "execute": 31.2, "compose": 1691.0, "verify": 0.3 },
  "models": ["gemini-2.5-flash", "gemini-2.5-flash"]
}
```

`answer`, `status`, `evidence`, `cost_usd` and `latency_ms` are the contract. `intent`,
`timings_ms`, `models` and `reason` are **extra fields** beyond it.

`models` names the provider model that served each of the request's two calls, in order. An
entry other than the configured `LLM_MODEL` means that call fell back (see below), so an
answer produced under degradation is never reported as if it were not.

### The pipeline

Six stages, of which two call a model. Any of the four code stages can end the request on
its own, which is the point: a refusal decided before the router costs nothing.

| Stage | What it does | Can refuse with |
|---|---|---|
| `guard` | Screens input aimed at the system rather than the data | `blocked_input` |
| `resolve` | Matches entities and the period against the warehouse's own vocabularies | `unknown_entity`, `out_of_period`, `unsupported_metric` |
| `route` | **Model call 1.** Picks one of eight question families and the query shape | `no_route` |
| `execute` | Runs the family's parameterised SQL. **Every figure in the response originates here** | `no_route`, `no_rows` |
| `compose` | **Model call 2.** Writes prose from the evidence rows and nothing else | — |
| `verify` | Checks the premise, then every numeral in the prose against the rows | `false_premise`, `ungrounded_figure` |

The router never sees a figure, never names an entity and never picks a period — those were
resolved from the data before it was called. It returns one JSON object inside a closed
schema, and every field is re-checked against the intent catalogue on this side of the wire.

### The eight question families

| Family | Answers |
|---|---|
| Q1 | Target versus actual, ranked by the size of the gap |
| Q2 | Sales within one period, ranked by value or units |
| Q3 | One period against another, with the change between them |
| Q4 | Stock-outs, by distributor, SKU, brand or region |
| Q5 | Promotion uplift against the four weeks before each promotion |
| Q6 | What the FY26 documents say about a brand, region or month |
| Q7 | What to do about it — the playbook, via the same engine as `/actions` |
| Q8 | What the pack covers: brands, regions, SKUs, periods |

There is **no text-to-SQL**. A question outside these eight has no execution path at all,
which is what makes the refusal reliable rather than a matter of the model's judgement.

### Refusals

Every outcome is a `200`. `status` is `NO_ANSWER` and `reason` carries a stable token:

| Reason | When |
|---|---|
| `blocked_input` | The input is talking to the system rather than about the data |
| `unknown_entity` | A name the pack does not hold — refused even where the question also named something real |
| `out_of_period` | Outside July 2025 – June 2026 |
| `unsupported_metric` | Margin, market share, ROI, a forecast — nothing in the pack derives them |
| `no_route` | No family fits, or the chosen family cannot narrow by something the question named |
| `no_rows` | The query is valid and the warehouse holds nothing matching it |
| `false_premise` | The question assumed a direction of travel the rows contradict |
| `ungrounded_figure` | The drafted answer stated a number no evidence row supports, so it was withheld |
| `no_provider_key`, `provider_timeout`, `provider_rate_limited`, `provider_error` | The model could not be reached. The figures are unaffected — retry |

A withheld answer still returns its evidence rows: the rows the answer should have been
written from are more useful than nothing.

### Grounding

`verify` extracts every numeral from the prose and requires each one to be traceable to an
evidence row, the resolved period, or something the question itself named. A figure written
to fewer decimal places than it was computed to still matches — 93 may stand for 92.77,
because rounding a figure is restating it. Rescaling one is not: "2.2 crore" cannot stand
for 22002083, since that is arithmetic, and arithmetic belongs in SQL.

### Model fallback and the circuit breaker

One provider, a chain of models: `LLM_MODEL` first, then each entry of `LLM_FALLBACK_MODELS`
in order. A free-tier daily allowance is counted per *model id*, not per key —
`gemini-2.5-flash` grants about 20 requests a day and a full evaluation run needs up to 86 —
so the chain is what lets a run finish without a paid key. Both committed runs under
`eval/results/` prove the point: the primary's allowance ran out 14 calls into the first, and
the fallback carried the remaining 70 and the whole of the second.

A call steps to the next model only when the failure says the model is unavailable: `429`,
`503`, a timeout, any `5xx`, or a `404` (the provider does not serve that id). It does **not**
step across on a `400`, `401`, `403`, or a malformed response body. The chain shares one key,
one endpoint and one payload shape, so those would be refused identically by every candidate;
re-asking would spend three round-trips hiding one bug.

Because a `400` is not a fallback-worthy fault, a request field that only some models accept
would strand the chain on the first model that rejects it. `reasoning_effort` is exactly such
a field: Google documents that reasoning cannot be turned off for Gemini 2.5 Pro or any 3.x
model, and those return `400` for `"none"`. The client therefore sends each model the floor it
accepts — `none` to the 2.5 family, `minimal` to everything else. On 3.x the two are the same
floor, measured: a `minimal` call reports the same token total a `none` call does. On 2.5 they
are not, and `none` is the cheaper one, so neither value replaces the other.

Retry backoff is spent only on the last model in the chain. While a candidate remains,
stepping costs nothing and can still answer; sleeping costs the caller seconds and cannot beat
a quota measured per day.

Two consecutive faults open that model's circuit breaker for `LLM_BREAKER_COOLDOWN_S`, during
which the chain skips it with no call at all — an exhausted daily quota is then learned once
rather than once per question. One fault does not open it: a single 429 can be a per-minute
burst the next call clears. When the cooldown elapses the next call probes the model; success
closes the breaker, another fault re-opens it at once. A chain whose every breaker is open
still tries one model, so a cooldown can never itself become the outage.

```
LLM_MODEL=gemini-2.5-flash
LLM_FALLBACK_MODELS=gemini-3.1-flash-lite,gemini-3.5-flash-lite,gemini-3.6-flash,gemini-3.5-flash
LLM_BREAKER_THRESHOLD=2        # consecutive faults that open a breaker
LLM_BREAKER_COOLDOWN_S=300     # how long it stays open; 0 disables skipping
```

Leave `LLM_FALLBACK_MODELS` empty for single-model behaviour. Verify a chain before trusting
it, by calling each id with the shape this service sends. On a key issued in 2026,
`gemini-2.5-flash-lite` and `gemini-2.5-pro` both return `404` ("no longer available to new
users"), so neither belongs in a chain; the five ids above were each confirmed to answer, and
are ordered by list price ascending so a degraded run steps down to the cheapest model that
can still answer.

### Cost and latency

`cost_usd` is the provider's own reported token usage priced against a committed rate table
in `llm/pricing.py`. Rates are transcribed from the published price pages with the date each
was read, and committed rather than fetched — a figure this repository publishes must not
change because a web page did.

| Model | Input $/Mtok | Output $/Mtok |
|---|---|---|
| `gemini-2.5-flash` (default) | 0.30 | 2.50 |
| `gemini-2.5-flash-lite` | 0.10 | 0.40 |
| `gemini-2.5-pro` | 1.25 | 10.00 |
| `gemini-3.1-flash-lite` | 0.25 | 1.50 |
| `gemini-3.5-flash-lite` | 0.30 | 2.50 |
| `gemini-3.5-flash` | 1.50 | 9.00 |
| `gemini-3.6-flash` | 0.75 | 3.75 |
| `gpt-4o-mini` | 0.15 | 0.60 |
| `gpt-4o` | 2.50 | 10.00 |

Each call is priced against the model that **served** it, so a request that fell back reports
the cost it incurred rather than the primary's. A model with no card reports `0.0` and logs
one warning — never a guessed rate. Gemini bills
reasoning tokens as output but reports them only inside `total_tokens`, so output is priced
at the wider of `completion_tokens` and `total_tokens − prompt_tokens`.

`latency_ms` spans the whole handler on a monotonic clock; `timings_ms` breaks it down by
stage. A stage entered twice accumulates, so a retried provider call reports the time the
caller actually waited.

## Evaluation

```bash
python eval/run_eval.py --base-url http://127.0.0.1:8000
```

[`eval/questions.yaml`](eval/questions.yaml) holds the labelled set — all eight families,
every refusal class, with paraphrase variants. **Every expected figure was computed from the
warehouse with hand-written SQL before the case was written**, never read back out of an
answer; a case that encodes what the system said measures nothing.

The runner grades the evidence, never the prose: two correct answers can be worded
differently and neither is more correct for it. A provider outage is reported separately and
excluded from the denominator — a 429 says nothing about whether the question would have
been routed correctly. Results are written to `eval/results/`.

`--pace` sets the seconds between questions; the default of 12 keeps the free tier's
per-minute throttle out of the way, given two calls per question. It does nothing for the
**daily** cap, which pacing cannot solve — the free allowance for `gemini-2.5-flash` is 20
requests a day. A full run needs up to 86: of the 57 cases, 14 refuse before a model is
reached at all, four cost one call and the remaining 39 cost two. Point `LLM_MODEL` at a model
with a larger free allowance, set `LLM_FALLBACK_MODELS` so the service steps down when the
primary runs out, or use a paid key.

A run that fell back still measures the system, but a different configuration of it, so the
summary carries `calls_by_model`. More than one entry means the accuracy figure is not any one
model's, and the runner prints that alongside it. Both committed runs show this in practice —
the first was served by two models and the second by one — and
[ARTEFACT.md](ARTEFACT.md) §5 reports the figures with the models that produced them.

The runner abandons a run after three provider failures in a row (`--max-consecutive-faults`,
0 to disable). A quota measured per day does not clear part-way through a run, and neither
does a service that is down; grinding through the rest would spend an hour producing a file
of skips. Use `--only` to run one family.

---

## Development

```bash
make lint      # ruff check + ruff format --check
make format    # auto-fix and format
make test      # unit and integration tests with coverage
make check     # everything CI runs
```

On Windows, run the underlying commands directly (`ruff check .`, `pytest --cov`, and so on).

Tests split by marker: `pytest -m "not integration"` runs the fast unit suite;
`pytest -m integration` exercises the real data pack and the HTTP app. **No test requires a
live LLM key**, and none makes a network call.

CI runs lint, format, `python prepare.py`, the tests, and `git diff --exit-code -- data/` to
prove the provided pack is unchanged.

## Repository layout

```
.
├── prepare.py                  # one-command preparation (shim → acpl_assistant.prepare.cli)
├── data/<pack>/                # provided data pack — read-only, committed for reproducibility
├── eval/                       # labelled cases, runner, committed results
├── src/acpl_assistant/
│   ├── config.py               # typed settings from env
│   ├── schemas.py              # request/response models
│   ├── service.py              # FastAPI app, /actions, /health, main()
│   ├── prepare/                # cli · load · conform · warehouse
│   ├── ask/                    # guard · resolve · router · intents · execute · compose · verify · pipeline
│   ├── actions/                # rules · engine
│   ├── llm/                    # client · pricing
│   └── obs/                    # meter
└── tests/
    ├── unit/                   # pure functions and synthetic warehouses
    └── integration/            # prepared warehouse + HTTP app (no live LLM)
```

`prepare/` never imports from `ask/` or `actions/`; `actions/` never imports `llm/` — the
rules engine is LLM-free by design. Both boundaries are asserted by a test, not just stated
here.
