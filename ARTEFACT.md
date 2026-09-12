# ARTEFACT — self-audit

What calling `/ask` and `/actions` does not show: what the system actually holds, what it had
to reconcile to hold it, and how well it answers. Every figure below is produced by
[`prepare.py`](prepare.py) and read from the committed
[`prep_report.json`](prep_report.json); none is typed by hand.

Reproduce all of it with one command:

```bash
python prepare.py
```

**Status.** Sections 1 to 3 are complete and verified. Section 4 is pending: the `/ask`
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

## 4. Evaluation — pending

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

## 5. How this was checked

Preparation is gated, not merely reported. `python prepare.py` asserts twelve row counts and
all seven reconciliation items, and exits non-zero if any moves, so a changed input fails the
build instead of quietly shifting a figure published here.

| Check | Result |
|---|---|
| Test suite | 71 passing |
| Integration tests against the real pack | 16 |
| Lint and formatting | clean |
| Data pack modified | no, asserted by digest and by `git diff` |
| Report determinism | two runs produce byte-identical JSON |

The suite verifies the failure paths too, not only the happy path: a wrong row count, a missing
table and a failed reconciliation item each produce a gate failure.
