# ACPL Sales Data — Data Dictionary

Seven CSV exports covering financial year FY26 (1 July 2025 – 30 June 2026). All monetary values are INR. Each file comes from a different system and is provided exactly as exported.

## fact_primary_sales.csv — primary-sales ledger (weekly)
One row per SKU × territory × week. 52 weeks; `week_start` runs from 2025-07-01 in 7-day steps.

| column | type | meaning |
|---|---|---|
| `week_start` | date (YYYY-MM-DD) | first day of the sales week. A week belongs to the calendar month containing its `week_start`. |
| `sku_code` | string | product code — see `dim_sku.csv` |
| `territory_code` | string | sales territory — see `dim_geo.csv` |
| `units` | integer | units shipped from ACPL to distributors in the territory that week |
| `value_inr` | decimal | primary sales value of those units, INR |

## fact_targets.csv — target plan (monthly)
One row per brand × region × month.

| column | type | meaning |
|---|---|---|
| `month` | string (YYYY-MM) | target month |
| `brand_name` | string | brand, as named in `dim_sku.csv` |
| `region_name` | string | region, as named in `dim_geo.csv` |
| `target_value_inr` | integer | primary-sales value target for the month, INR |

## stockouts.csv — distributor stock-out log
One row per distributor × SKU × week in which the distributor reported zero stock on at least one day.

| column | type | meaning |
|---|---|---|
| `distributor_id` | string | distributor — see `dim_distributor.csv` |
| `item_code` | string | product code, same values as `sku_code` in `dim_sku.csv` |
| `week_start` | date (YYYY-MM-DD) | week of the stock-out |
| `region` | string | region as entered in the distributor portal |
| `days_out_of_stock` | integer | number of days in the week with zero stock (1–7) |

## promotions.csv — trade-promotion calendar
One row per promotion: a SKU, a region and a date window.

| column | type | meaning |
|---|---|---|
| `promo_id` | string | promotion identifier |
| `sku` | string | product code, same values as `sku_code` in `dim_sku.csv` |
| `region` | string | region in which the promotion ran |
| `start_date` | string (DD/MM/YYYY) | first day of the promotion |
| `end_date` | string (DD/MM/YYYY) | last day of the promotion (inclusive) |
| `discount_pct` | integer | trade discount offered, % |
| `mechanic` | string | promotion type: Price-off, Buy 2 Get 1, Extra 20% volume, Combo pack |

## dim_sku.csv — product master
120 SKUs: 5 categories × 3 brands × 8 pack sizes.

| column | type | meaning |
|---|---|---|
| `sku_code` | string | product code (primary key) |
| `sku_name` | string | brand + pack size |
| `brand` | string | brand |
| `category` | string | Biscuits, Snacks, Beverages, Home Care, Personal Care |
| `pack_size` | string | pack description |
| `mrp_inr` | integer | maximum retail price, INR |

## dim_geo.csv — geography master
12 territories, 3 per region.

| column | type | meaning |
|---|---|---|
| `territory_code` | string | territory code (primary key) |
| `territory_name` | string | territory (city) name |
| `region` | string | North, South, East, West |
| `state` | string | state |

## dim_distributor.csv — distributor master
40 distributors, 3–4 per territory.

| column | type | meaning |
|---|---|---|
| `distributor_id` | string | distributor id (primary key) |
| `distributor_name` | string | trading name |
| `territory_code` | string | territory served — see `dim_geo.csv` |
| `city` | string | city |

## documents/ — working documents (Word, .docx)
A small pack of ACPL's own internal documents. Some carry context an answer or an action needs; some are routine and carry nothing. They are ordinary working files, not curated data.

| file | what it is |
|---|---|
| `visit_note_north_feb2026.docx` | a regional market-visit note |
| `escalation_sop.docx` | the sales-operations escalation procedure |
| `promo_circular_h2fy26.docx` | the trade-promotions circular for H2 FY26 |
| `distributor_note_west.docx` | a West-region distributor supply note |
| `weekly_summary_w32.docx` | a routine weekly sales summary |
| `hr_circular.docx` | an HR leave-calendar circular |

## action_playbook.xlsx — the action playbook (Excel)
One sheet, `playbook`, one row per rule. A recommended action must trace to a rule here.

| column | meaning |
|---|---|
| `rule_id` | stable rule identifier (e.g. `R-01`) — cite this on any action that follows the rule |
| `condition` | the business situation the rule applies to |
| `recommendation` | how to read the situation |
| `action` | the action to recommend |
| `needs_approval` | `Yes` if the action notifies another team or changes a commitment (must be gated before it is carried out); `No` for analysis/review actions |
