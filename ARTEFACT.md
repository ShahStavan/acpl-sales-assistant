# ARTEFACT — self-audit

What calling `/ask` and `/actions` does not show: what the system actually holds, what it had
to reconcile to hold it, and how well it answers. Every figure below is produced by
[`prepare.py`](prepare.py) and read from the committed
[`prep_report.json`](prep_report.json); none is typed by hand.

Reproduce all of it with one command:

```bash
python prepare.py
```

**Status.** Sections 1 to 4 are complete and verified. Section 5 is pending: the `/ask`
pipeline and its evaluation are not built yet, so no accuracy, cost or latency figure is
claimed. An empty row is stated as empty rather than filled with a placeholder.

## 1. Rows held from each file after preparation

Source rows read, then rows the warehouse holds. They agree for every provided file: nothing
is dropped, deduplicated or filtered during preparation.

| Source file | Rows read | Rows held |
|---|---:|---:|
| `fact_primary_sales.csv` | 74,880 | 74,880 |
| `fact_targets.csv` | 720 | 720 |
| `stockouts.csv` | 520 | 520 |
| `promotions.csv` | 40 | 40 |
| `dim_sku.csv` | 120 | 120 |
| `dim_geo.csv` | 12 | 12 |
| `dim_distributor.csv` | 40 | 40 |
| `documents/*.docx` | 6 | 6 |
| `action_playbook.xlsx` | 8 | 8 |

Three further tables are derived, not read, and exist so that no question has to recompute a
grain at query time:

| Derived table | Rows | What it is |
|---|---:|---|
| `sales_by_brand_region_week` | 3,120 | sales rolled up through both masters |
| `sales_by_brand_region_month` | 720 | the same, rolled to the target grain |
| `v_achievement` | 720 | a view joining actuals to targets |

Achievement is a view rather than a table because it is a pure join of two tables already
written. Materialising it would duplicate 720 rows and let the copies drift.

## 2. National FY26 primary-sales value total

```
INR 1,357,631,078.74
```

The ledger covers FY26 and nothing else, so this is the sum of `value_inr` across all 74,880
rows. The integration suite recomputes it in SQL directly against the warehouse and asserts
the two agree, so the published figure does not depend on the pandas path that produced it.

## 3. Cross-source mismatches reconciled

Found across the seven exports, the six documents and the playbook. All resolved in code, in
[`prepare/conform.py`](src/acpl_assistant/prepare/conform.py); no provided file is edited, and
continuous integration fails on any diff under `data/`.

| # | Mismatch found | How it was resolved | Verified |
|---|---|---|---|
| 1 | The SKU key is spelled three ways: `sku_code` in sales and the product master, `sku` in promotions, `item_code` in stock-outs | Conformed all three to `sku_code` at load, then checked every fact row against the master | 0 orphans across 74,880 + 520 + 40 rows |
| 2 | Targets name their dimensions `brand_name` and `region_name`; every other file uses `brand` and `region` | Conformed to the dimension spelling, which the masters own | 720 of 720 target rows conformed |
| 3 | `stockouts.region` is free text typed into the distributor portal, holding 16 spellings of 4 regions (`North`, `NORTH`, `north`, `North Region`, and the same for the other three) | Derived the true region through `distributor_id` to `territory_code` to the geography master, used the portal text only to cross-check, then dropped it | 0 conflicts across 520 rows |
| 4 | Three date formats in three files: ISO in sales and stock-outs, `YYYY-MM` in targets, `DD/MM/YYYY` in promotions | Parsed each with an explicit format, never inferred, so `01/07/2025` cannot become 7 January | The first promotion parses as 1 July 2025 |
| 5 | Grain disagreement: sales are SKU by territory by week, targets are brand by region by month | Rolled sales up through both masters, assigning each week to the month containing its `week_start`, which is the data dictionary's own rule | 720 of 720 cells join, 0 orphans on either side |
| 6 | Promotions are a SKU by region by date window, which matches no reporting period in any other file | Matched to sales weeks by date-range overlap rather than by month equality | 40 matched, 39 measurable |
| 7 | Playbook thresholds are English prose in a spreadsheet cell, not numbers | Parsed once at preparation into typed rule objects carrying the approval flag | 8 rules parsed, 3 gated |
| 8 | Documents name months without a year, and plain substring matching tagged the February visit note with "Mar" out of the word "Market" | Matched whole words only, and resolved a month to a `YYYY-MM` key only where the text or the file name supplies a year | The visit note resolves to `2026-02` and carries no March |

One promotion of the forty is not measurable. It starts in the first week of the fiscal year,
so no prior period exists to compare it against. The reconciliation records this as the reason
rather than counting it as a failure, and a promotion that is unmeasurable for any other reason
still fails the check.

The West distributor note names an "April to June" quarter with no year anywhere in the text or
its file name. Its months are therefore left unresolved rather than assumed to be FY26. This is
a deliberate choice: the fiscal year is knowable from context, but guessing it in the loader
would put an unsourced figure into evidence.

## 4. Actions the playbook yields on this data

All eight rules evaluated over FY26 by [`actions/rules.py`](src/acpl_assistant/actions/rules.py),
grouped by the entity each action targets. No model is involved, so these counts are
reproducible from `warehouse.duckdb` alone; every one is asserted in the integration suite.

| Rule | Approval | Findings | Actions after grouping | What fired |
|---|---|---:|---:|---|
| R-01 | **PENDING_APPROVAL** | 3 | 1 | Aqualite in the West, April to June, all three months at 62% of target with 3, 4 and 2 stock-out weeks |
| R-02 | RECOMMENDED | 0 | 0 | No case — no promotion is weak enough |
| R-03 | RECOMMENDED | 1 | 1 | CremeDelight in the North, February, 72%, with the visit note covering it |
| R-04 | **PENDING_APPROVAL** | 2 | 2 | D032 and D033 on BV-0104, 9 weeks each, 14 April to 9 June |
| R-05 | RECOMMENDED | 0 | 0 | No case — the best cell in FY26 is 108.7% |
| R-06 | RECOMMENDED | 1 | 1 | MintGuard in the East, March, 74%, with no note to explain it |
| R-07 | RECOMMENDED | 23 | 23 | 23 of 39 measurable promotions beat 25% uplift |
| R-08 | **PENDING_APPROVAL** | 47 | 27 | 47 distributor-months collapse to one stock-review call per distributor |
| | | **77** | **55** | `{"scope": "all"}` returns 55 actions, uncapped |

Two rules report nothing. That is the finding, not a gap: **R-02** requires a promotion
running under 10% uplift while the brand misses target, and the weakest of the 39 measurable
promotions delivered 13.5%; **R-05** requires achievement above 110%, and the best cell in
the year reached 108.7%. Both remain implemented, and both are proven to fire on synthetic
data built to cross their thresholds — without that, "no qualifying case" and "dead code"
would look identical from the outside.

Uplift is measured against the four weeks preceding each promotion, for the same SKU and
region. Thirty-seven promotions have a full four; one has three and one has two, because the
fiscal year starts. The fortieth opens in the first week of the ledger with no prior period
at all, and is reported as unmeasurable rather than counted as zero. Across the 39, uplift
runs from 13.5% to 44.7%.

The roll-up to SKU by region by week has to happen *before* those four weeks are taken.
Sales are held at SKU by territory by week and a region holds three territories, so reading
the baseline as the last four *rows* would compare a promotion against barely more than one
week of trading and report uplifts of 63% to 131% instead. A test holds that boundary.

### What the figures are grounded in

| Property | How it is enforced |
|---|---|
| Every numeral in a `finding` also appears in that action's `evidence` or period | Asserted over all 55 actions; the check itself is tested against an invented figure so it cannot pass vacuously |
| `state` comes from the playbook, not the code | Asserted by inverting every `needs_approval` flag and requiring every state to invert with it |
| `action` text is ACPL's wording | Read from `action_playbook.xlsx`, not written here |
| The same warehouse gives the same list | Two runs compared byte for byte. Every rule query carries an explicit `ORDER BY`: DuckDB aggregates in parallel and would otherwise return evidence in a different order each call |
| No action is lost or double-counted by scope | The four regions' lists union to exactly the `all` list |
| The rules engine never calls a model | `actions/` is asserted not to import `llm/`, by scanning the imports rather than trusting the convention |

## 5. Evaluation — pending

Not yet measurable. The `/ask` pipeline is not built, so there is no accuracy to report, no gap
to have closed, and no cost or latency to have observed. This section will state first accuracy
measured, the largest gap found, the single change made to close it, accuracy after that change,
median cost per question, and p50 and p95 latency.

| Figure | Value |
|---|---|
| Accuracy, first measured | pending |
| Largest gap found | pending |
| The one change made | pending |
| Accuracy after | pending |
| Median cost per question | pending |
| Latency p50 / p95 | pending |

## 6. How this was checked

Preparation is gated, not merely reported. `python prepare.py` asserts twelve row counts and
all seven reconciliation items, and exits non-zero if any moves, so a changed input fails the
build instead of quietly shifting a figure published here.

| Check | Result |
|---|---|
| Test suite | 219 passing |
| Integration tests against the real pack and the HTTP app | 76 |
| Statement and branch coverage | 98% |
| Lint and formatting | clean |
| Data pack modified | no, asserted by digest, by `git diff`, and by `data/** -text` in `.gitattributes` |
| Report determinism | two runs produce byte-identical JSON, with LF endings on every platform |
| `/actions` determinism | two calls produce identical responses |
| Concurrent `/actions` requests | 16 in parallel return identical bodies; each request gets its own cursor |
| Live LLM key required by any test | none |

The suite verifies the failure paths too, not only the happy path: a wrong row count, a missing
table and a failed reconciliation item each produce a gate failure; an unresolvable scope
returns an empty list rather than a guess; and a service with no warehouse reports `degraded`
rather than pretending to be healthy.
