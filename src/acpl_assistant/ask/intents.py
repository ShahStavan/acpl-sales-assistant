"""The eight intent families and the parameterised SQL each one runs.

One intent per question class (DESIGN.md §2.2, §4.1). There is no text-to-SQL: a question
that maps to none of these has no execution path, which is what makes a refusal reliable
rather than a matter of prompt discipline.

Every query here is a static string with bound parameters. The only text ever interpolated
into SQL is a column expression drawn from the dimension maps or a filter template drawn
from the filter maps — both fixed at import time, neither reachable from the question or
from model output. Slot *values* are always bound, never spliced.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from acpl_assistant.actions.rules import (
    PROMOTION_UPLIFT_SQL,
    PROMOTIONS_FILE,
    SALES_FILE,
    STOCKOUTS_FILE,
    TARGETS_FILE,
)

if TYPE_CHECKING:
    from acpl_assistant.ask.resolve import Entities, Period

# Achievement is a join of the sales roll-up and the targets file, so a row carrying both
# figures is attributed to both. Naming one alone would make half of it untraceable.
ACHIEVEMENT_FILE = f"{SALES_FILE} + {TARGETS_FILE}"

# Rows that carry their own ``source_file`` column: documents name the file they are, and
# the coverage query names the master each family was read from.
PER_ROW_SOURCE = ""

# ---------------------------------------------------------------------------
# Slot vocabulary — the closed sets the router may choose from
# ---------------------------------------------------------------------------

METRICS = ("value", "units")
DIRECTIONS = ("largest", "smallest")

TOP_N_DEFAULT = 5
TOP_N_MIN = 1

# A ranking longer than this is not an answer a salesperson reads, and every row is carried
# in ``evidence`` and re-checked by the verifier, so the cap bounds the response too.
TOP_N_MAX = 25

INTENT_IDS = ("Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7", "Q8")
NO_INTENT = "NONE"


@dataclass(frozen=True)
class Slots:
    """Everything a query needs, after the router's choices have been re-validated.

    Entities and periods come from :mod:`acpl_assistant.ask.resolve`, which read them out
    of the warehouse's own vocabularies; the router only chooses among them.
    """

    entities: Entities
    period: Period
    compare_to: Period | None = None
    metric: str = "value"
    dimension: str = ""
    """Left empty unless the router names one; each intent then applies its own default."""
    direction: str = "largest"
    top_n: int = TOP_N_DEFAULT


@dataclass(frozen=True)
class Query:
    """One statement ready to execute, with the file its rows are attributed to."""

    sql: str
    params: list[Any] = field(default_factory=list)
    source_file: str = PER_ROW_SOURCE
    """Attribution for every row, or ``""`` when each row carries its own."""


# ---------------------------------------------------------------------------
# SQL assembly
# ---------------------------------------------------------------------------

# Marker a filter template carries where its bound placeholders go. A template may hold it
# more than once — "this SKU name or this SKU code" — in which case the values bind again.
PH = "{ph}"


def _placeholders(count: int) -> str:
    """Render *count* bound placeholders, ``?, ?, ?``."""
    return ", ".join(["?"] * count)


def _render(template: str, values: Sequence[str]) -> tuple[str, list[Any]]:
    """Expand a filter template against *values*, returning the clause and its parameters."""
    marks = template.count(PH)
    clause = template.replace(PH, _placeholders(len(values)))
    return clause, list(values) * marks


def _entity_filters(
    entities: Entities, templates: dict[str, str]
) -> tuple[list[str], list[Any], list[str]]:
    """Turn the named entities into WHERE clauses under one intent's filter map.

    Returns the clauses, their bound parameters, and the families the intent has no way to
    narrow by. That third list is not discarded: an intent that cannot honour something the
    question named must not answer as though it had, so the executor refuses instead.
    """
    clauses: list[str] = []
    params: list[Any] = []
    unhonoured: list[str] = []
    for family, values in entities.as_dict().items():
        template = templates.get(family)
        if template is None:
            unhonoured.append(family)
            continue
        clause, bound = _render(template, values)
        clauses.append(clause)
        params.extend(bound)
    return clauses, params, unhonoured


def _where(clauses: Sequence[str]) -> str:
    """Join clauses into a WHERE block, or nothing at all when there are none."""
    return f"WHERE {' AND '.join(clauses)}\n" if clauses else ""


def _order(expression: str, direction: str) -> str:
    """Render an ORDER BY from the validated direction slot.

    The slot names the measure rather than a verdict: the worst distributor has the
    *largest* days out of stock, and a good/bad enum would have the router choose "bottom"
    for it and rank the least affected first.
    """
    return f"{expression} {'DESC' if direction == 'largest' else 'ASC'}"


def _limit(top_n: int) -> int:
    """Clamp the row cap into the range the response and the verifier are sized for."""
    return max(TOP_N_MIN, min(int(top_n), TOP_N_MAX))


# ---------------------------------------------------------------------------
# Dimensions — the breakdowns an intent may group by, as (expression, alias)
# ---------------------------------------------------------------------------

Columns = tuple[tuple[str, str], ...]

SALES_DIMENSIONS: dict[str, Columns] = {
    "brand": (("sk.brand", "brand"),),
    "category": (("sk.category", "category"),),
    "sku": (("sk.sku_code", "sku_code"), ("sk.sku_name", "sku_name")),
    "pack_size": (("sk.brand", "brand"), ("sk.pack_size", "pack_size")),
    "region": (("g.region", "region"),),
    "territory": (("g.region", "region"), ("g.territory_name", "territory_name")),
    "brand_region": (("sk.brand", "brand"), ("g.region", "region")),
    "month": (("strftime(f.week_start, '%Y-%m')", "month"),),
}

ACHIEVEMENT_DIMENSIONS: dict[str, Columns] = {
    "brand_region": (("a.brand", "brand"), ("a.region", "region")),
    "brand": (("a.brand", "brand"),),
    "region": (("a.region", "region"),),
    "month": (("a.month", "month"),),
}

STOCKOUT_DIMENSIONS: dict[str, Columns] = {
    "distributor_sku": (
        ("s.distributor_id", "distributor_id"),
        ("d.distributor_name", "distributor_name"),
        ("s.sku_code", "sku_code"),
        ("sk.sku_name", "sku_name"),
        ("s.region", "region"),
    ),
    "distributor": (
        ("s.distributor_id", "distributor_id"),
        ("d.distributor_name", "distributor_name"),
        ("s.region", "region"),
    ),
    "sku": (("s.sku_code", "sku_code"), ("sk.sku_name", "sku_name"), ("sk.brand", "brand")),
    "brand": (("sk.brand", "brand"), ("s.region", "region")),
    "region": (("s.region", "region"),),
}


def _select(columns: Columns) -> str:
    """Render grouping columns as a SELECT list."""
    return ", ".join(f"{expression} AS {alias}" for expression, alias in columns)


def _group(columns: Columns) -> str:
    """Render the matching GROUP BY, by position."""
    return ", ".join(str(i) for i in range(1, len(columns) + 1))


def _tiebreak(columns: Columns) -> str:
    """Order the grouping columns after the metric, so equal figures still sort stably."""
    return ", ".join(alias for _, alias in columns)


def _dimension(available: dict[str, Columns], requested: str, fallback: str) -> Columns:
    """Resolve the requested breakdown, falling back when the intent does not offer it.

    A breakdown the intent cannot produce changes the grain of the answer, not the truth of
    its figures: every row returned names the columns it was grouped by, and the verifier
    still checks each number in the prose against those rows.
    """
    return available.get(requested) or available[fallback]


# ---------------------------------------------------------------------------
# Filter maps — one per source shape
# ---------------------------------------------------------------------------

# ``skus``, ``territories`` and ``distributors`` each hold either a name or a code, because
# the resolver accepts both spellings; matching on both columns costs one more bound copy
# of the same values and removes the need to know which was written.
SALES_FILTERS: dict[str, str] = {
    "brands": "sk.brand IN ({ph})",
    "categories": "sk.category IN ({ph})",
    "skus": "(sk.sku_name IN ({ph}) OR sk.sku_code IN ({ph}))",
    "pack_sizes": "sk.pack_size IN ({ph})",
    "regions": "g.region IN ({ph})",
    "territories": "(g.territory_name IN ({ph}) OR g.territory_code IN ({ph}))",
}

# Achievement is held at brand × region × month, so a narrower entity narrows it only by
# way of the master that relates the two: a category becomes the brands in it.
ACHIEVEMENT_FILTERS: dict[str, str] = {
    "brands": "a.brand IN ({ph})",
    "regions": "a.region IN ({ph})",
    "categories": "a.brand IN (SELECT brand FROM dim_sku WHERE category IN ({ph}))",
    "skus": (
        "a.brand IN (SELECT brand FROM dim_sku WHERE sku_name IN ({ph}) OR sku_code IN ({ph}))"
    ),
    "pack_sizes": "a.brand IN (SELECT brand FROM dim_sku WHERE pack_size IN ({ph}))",
    "territories": (
        "a.region IN (SELECT region FROM dim_geo "
        "WHERE territory_name IN ({ph}) OR territory_code IN ({ph}))"
    ),
}

STOCKOUT_FILTERS: dict[str, str] = {
    "brands": "sk.brand IN ({ph})",
    "categories": "sk.category IN ({ph})",
    "skus": "(sk.sku_name IN ({ph}) OR sk.sku_code IN ({ph}))",
    "pack_sizes": "sk.pack_size IN ({ph})",
    "regions": "s.region IN ({ph})",
    "territories": "(g.territory_name IN ({ph}) OR g.territory_code IN ({ph}))",
    "distributors": "(d.distributor_id IN ({ph}) OR d.distributor_name IN ({ph}))",
}

PROMOTION_FILTERS: dict[str, str] = {
    "promo_ids": "u.promo_id IN ({ph})",
    "mechanics": "u.mechanic IN ({ph})",
    "brands": "u.brand IN ({ph})",
    "regions": "u.region IN ({ph})",
    "skus": (
        "u.sku_code IN (SELECT sku_code FROM dim_sku "
        "WHERE sku_name IN ({ph}) OR sku_code IN ({ph}))"
    ),
    "categories": "u.sku_code IN (SELECT sku_code FROM dim_sku WHERE category IN ({ph}))",
    "pack_sizes": "u.sku_code IN (SELECT sku_code FROM dim_sku WHERE pack_size IN ({ph}))",
    "territories": (
        "u.region IN (SELECT region FROM dim_geo "
        "WHERE territory_name IN ({ph}) OR territory_code IN ({ph}))"
    ),
}

# Documents carry their tags as comma-separated text, so a tag is matched as a whole list
# element rather than as a substring: a document tagged "North East" is not the North.
DOCUMENT_TAGS: dict[str, str] = {
    "brands": "brands",
    "regions": "regions",
    "distributors": "distributors",
}


def _tag_match(column: str, values: Sequence[str]) -> tuple[str, list[Any]]:
    """Match any of *values* against a comma-separated tag column."""
    parts = [f"list_contains(string_split({column}, ', '), ?)"] * len(values)
    return f"({' OR '.join(parts)})", list(values)


# ---------------------------------------------------------------------------
# Q1 — target vs actual, gap ranking
# ---------------------------------------------------------------------------


def build_q1(slots: Slots) -> tuple[Query, list[str]]:
    """Rank the gap between target and actual over the period.

    Targets exist in value only, so this family always reports rupees whatever the metric
    slot says: a units gap would have no target to be a gap against.
    """
    columns = _dimension(ACHIEVEMENT_DIMENSIONS, slots.dimension, "brand_region")
    clauses, filters, unhonoured = _entity_filters(slots.entities, ACHIEVEMENT_FILTERS)
    months = list(slots.period.months)
    clauses.insert(0, f"a.month IN ({_placeholders(len(months))})")
    sql = (
        f"SELECT {_select(columns)},\n"
        "       sum(a.actual_value_inr) AS actual_value_inr,\n"
        "       sum(a.target_value_inr) AS target_value_inr,\n"
        "       sum(a.target_value_inr) - sum(a.actual_value_inr) AS gap_value_inr,\n"
        "       sum(a.actual_value_inr) / nullif(sum(a.target_value_inr), 0)"
        " AS achievement_ratio,\n"
        "       ? AS period\n"
        "FROM v_achievement a\n"
        f"{_where(clauses)}"
        f"GROUP BY {_group(columns)}\n"
        f"ORDER BY {_order('gap_value_inr', slots.direction)}, {_tiebreak(columns)}\n"
        "LIMIT ?"
    )
    params = [slots.period.label, *months, *filters, _limit(slots.top_n)]
    return Query(sql=sql, params=params, source_file=ACHIEVEMENT_FILE), unhonoured


# ---------------------------------------------------------------------------
# Q2 — sales aggregation and ranking
# ---------------------------------------------------------------------------

SALES_FROM = (
    "FROM fact_primary_sales f\n"
    "JOIN dim_sku sk USING (sku_code)\n"
    "JOIN dim_geo g USING (territory_code)\n"
)

# A week belongs to the month its start date falls in — the same convention the conformed
# roll-up and therefore v_achievement use, so Q1 and Q2 cannot disagree about a period.
SALES_MONTH = "strftime(f.week_start, '%Y-%m')"


def build_q2(slots: Slots) -> tuple[Query, list[str]]:
    """Aggregate and rank primary sales over one period.

    Both measures are selected whichever one the question asked for, so an answer can name
    units alongside value without a second query and the verifier can ground either.
    """
    columns = _dimension(SALES_DIMENSIONS, slots.dimension, "brand")
    clauses, filters, unhonoured = _entity_filters(slots.entities, SALES_FILTERS)
    months = list(slots.period.months)
    clauses.insert(0, f"{SALES_MONTH} IN ({_placeholders(len(months))})")
    metric_alias = "value_inr" if slots.metric == "value" else "units"
    sql = (
        f"SELECT {_select(columns)},\n"
        "       sum(f.value_inr) AS value_inr,\n"
        "       sum(f.units)     AS units,\n"
        "       ? AS period\n"
        f"{SALES_FROM}"
        f"{_where(clauses)}"
        f"GROUP BY {_group(columns)}\n"
        f"ORDER BY {_order(metric_alias, slots.direction)}, {_tiebreak(columns)}\n"
        "LIMIT ?"
    )
    params = [slots.period.label, *months, *filters, _limit(slots.top_n)]
    return Query(sql=sql, params=params, source_file=SALES_FILE), unhonoured


# ---------------------------------------------------------------------------
# Q3 — period comparison
# ---------------------------------------------------------------------------


def build_q3(slots: Slots) -> tuple[Query, list[str]]:
    """Compare the same breakdown across two periods, in one pass.

    Both periods are summed in a single scan, so the two figures come from one snapshot of
    one table and the change between them is arithmetic on two columns rather than a
    subtraction the model has been asked to perform.
    """
    earlier = slots.period
    later = slots.compare_to or slots.period
    columns = _dimension(SALES_DIMENSIONS, slots.dimension, "brand")
    clauses, filters, unhonoured = _entity_filters(slots.entities, SALES_FILTERS)

    from_months = list(earlier.months)
    to_months = list(later.months)
    span = from_months + [m for m in to_months if m not in from_months]
    clauses.insert(0, f"{SALES_MONTH} IN ({_placeholders(len(span))})")

    from_ph = _placeholders(len(from_months))
    to_ph = _placeholders(len(to_months))
    column = "f.value_inr" if slots.metric == "value" else "f.units"
    suffix = "value_inr" if slots.metric == "value" else "units"
    from_arm = f"sum(CASE WHEN {SALES_MONTH} IN ({from_ph}) THEN {column} ELSE 0 END)"
    to_arm = f"sum(CASE WHEN {SALES_MONTH} IN ({to_ph}) THEN {column} ELSE 0 END)"
    sql = (
        f"SELECT {_select(columns)},\n"
        "       ? AS from_period,\n"
        "       ? AS to_period,\n"
        f"       {from_arm} AS from_{suffix},\n"
        f"       {to_arm} AS to_{suffix},\n"
        f"       {to_arm} - {from_arm} AS delta_{suffix},\n"
        f"       ({to_arm} / nullif({from_arm}, 0)) - 1 AS change_ratio\n"
        f"{SALES_FROM}"
        f"{_where(clauses)}"
        f"GROUP BY {_group(columns)}\n"
        f"ORDER BY {_order(f'delta_{suffix}', slots.direction)}, {_tiebreak(columns)}\n"
        "LIMIT ?"
    )
    # Parameters follow the order the placeholders appear in the statement: the two labels,
    # then each CASE arm as written, then the span in the WHERE, the filters and the cap.
    arms = from_months + to_months + to_months + from_months + to_months + from_months
    params = [
        earlier.label,
        later.label,
        *arms,
        *span,
        *filters,
        _limit(slots.top_n),
    ]
    return Query(sql=sql, params=params, source_file=SALES_FILE), unhonoured


# ---------------------------------------------------------------------------
# Q4 — stock-out analysis
# ---------------------------------------------------------------------------

STOCKOUT_FROM = (
    "FROM stockouts s\n"
    "JOIN dim_distributor d USING (distributor_id)\n"
    "JOIN dim_sku sk USING (sku_code)\n"
    "JOIN dim_geo g ON g.territory_code = d.territory_code\n"
)


def build_q4(slots: Slots) -> tuple[Query, list[str]]:
    """Rank stock-out exposure over the period.

    Weeks are counted distinctly rather than summed: the ledger holds one row per
    distributor × SKU × week, and a distributor with four SKUs out in the same week has
    been out for one week, not four.
    """
    columns = _dimension(STOCKOUT_DIMENSIONS, slots.dimension, "distributor_sku")
    clauses, filters, unhonoured = _entity_filters(slots.entities, STOCKOUT_FILTERS)
    months = list(slots.period.months)
    clauses.insert(0, f"strftime(s.week_start, '%Y-%m') IN ({_placeholders(len(months))})")
    sql = (
        f"SELECT {_select(columns)},\n"
        "       count(DISTINCT s.week_start) AS weeks_out,\n"
        "       sum(s.days_out_of_stock)     AS days_out_of_stock,\n"
        "       strftime(min(s.week_start), '%Y-%m-%d') AS first_week,\n"
        "       strftime(max(s.week_start), '%Y-%m-%d') AS last_week,\n"
        "       ? AS period\n"
        f"{STOCKOUT_FROM}"
        f"{_where(clauses)}"
        f"GROUP BY {_group(columns)}\n"
        f"ORDER BY {_order('days_out_of_stock', slots.direction)}, {_tiebreak(columns)}\n"
        "LIMIT ?"
    )
    params = [slots.period.label, *months, *filters, _limit(slots.top_n)]
    return Query(sql=sql, params=params, source_file=STOCKOUTS_FILE), unhonoured


# ---------------------------------------------------------------------------
# Q5 — promotion effectiveness
# ---------------------------------------------------------------------------


def build_q5(slots: Slots) -> tuple[Query, list[str]]:
    """Rank promotions by uplift against their own pre-promotion baseline.

    The uplift definition is imported from :mod:`acpl_assistant.actions.rules` rather than
    restated, so the figure an answer quotes and the figure rule R-07 fires on are the same
    number computed once.
    """
    clauses, filters, unhonoured = _entity_filters(slots.entities, PROMOTION_FILTERS)
    # Overlap, not containment: a promotion that ran across a period boundary still ran in
    # the period, and reporting it only for the quarter it ended in would lose it.
    clauses.insert(0, "u.start_date <= ? AND u.end_date >= ?")
    sql = (
        "SELECT u.promo_id, u.sku_code, u.brand, u.region, u.mechanic, u.discount_pct,\n"
        "       strftime(u.start_date, '%Y-%m-%d') AS start_date,\n"
        "       strftime(u.end_date, '%Y-%m-%d')   AS end_date,\n"
        "       u.promo_weeks, u.baseline_weeks,\n"
        "       u.promo_avg_value_inr, u.baseline_avg_value_inr, u.uplift,\n"
        "       ? AS period\n"
        f"FROM ({PROMOTION_UPLIFT_SQL}) u\n"
        f"{_where(clauses)}"
        f"ORDER BY {_order('u.uplift', slots.direction)}, u.promo_id\n"
        "LIMIT ?"
    )
    params = [
        slots.period.label,
        slots.period.end_date,
        slots.period.start_date,
        *filters,
        _limit(slots.top_n),
    ]
    return Query(sql=sql, params=params, source_file=PROMOTIONS_FILE), unhonoured


# ---------------------------------------------------------------------------
# Q6 — document-grounded cause and policy
# ---------------------------------------------------------------------------

# The corpus holds six documents. More than this in one answer is not context, it is the
# whole pack pasted back.
DOCUMENT_LIMIT = 4


def build_q6(slots: Slots) -> tuple[Query, list[str]]:
    """Return the tagged documents that bear on the entities and period named.

    Documents are ranked by how many of the question's tags they carry rather than filtered
    to an exact conjunction: a note tagged with the brand but not the month is still the
    note that explains the month.
    """
    scores: list[str] = []
    params: list[Any] = []
    named = slots.entities.as_dict()
    for family, column in DOCUMENT_TAGS.items():
        values = named.get(family)
        if not values:
            continue
        clause, bound = _tag_match(column, values)
        scores.append(f"CASE WHEN {clause} THEN 1 ELSE 0 END")
        params.extend(bound)

    months = list(slots.period.months)
    scores.append(
        "CASE WHEN list_has_any(string_split(months_resolved, ', '), "
        f"[{_placeholders(len(months))}]) THEN 1 ELSE 0 END"
    )
    params.extend(months)

    # Only once something has been named is an untagged document uninformative; with no
    # tags at all the whole six-row corpus is the honest answer.
    matched = "WHERE tag_matches > 0\n" if named else ""
    # ``tag_matches`` ranks inside the subquery and is dropped from the projection: it is
    # this module's arithmetic, not a fact about the pack, and evidence carries only facts.
    sql = (
        "SELECT source_file, text, brands, regions, distributors, months_resolved FROM (\n"
        "  SELECT source_file, text, brands, regions, distributors, months_resolved,\n"
        f"         {' + '.join(scores)} AS tag_matches\n"
        "  FROM documents\n"
        ")\n"
        f"{matched}"
        "ORDER BY tag_matches DESC, source_file\n"
        "LIMIT ?"
    )
    return Query(sql=sql, params=[*params, DOCUMENT_LIMIT], source_file=PER_ROW_SOURCE), []


# ---------------------------------------------------------------------------
# Q8 — coverage and metadata
# ---------------------------------------------------------------------------

# Every figure here is counted from the masters at request time. A coverage answer quoting
# a constant would be the one answer in the system that could fall out of date silently.
COVERAGE_SQL = """
SELECT 'brands' AS family, count(DISTINCT brand) AS n_values,
       string_agg(DISTINCT brand, ', ' ORDER BY brand) AS value_list,
       'dim_sku.csv' AS source_file
FROM dim_sku
UNION ALL
SELECT 'categories', count(DISTINCT category), string_agg(DISTINCT category, ', ' ORDER BY category),
       'dim_sku.csv'
FROM dim_sku
UNION ALL
SELECT 'skus', count(DISTINCT sku_code), NULL, 'dim_sku.csv'
FROM dim_sku
UNION ALL
SELECT 'regions', count(DISTINCT region), string_agg(DISTINCT region, ', ' ORDER BY region), 'dim_geo.csv'
FROM dim_geo
UNION ALL
SELECT 'territories', count(DISTINCT territory_name),
       string_agg(DISTINCT territory_name, ', ' ORDER BY territory_name), 'dim_geo.csv'
FROM dim_geo
UNION ALL
SELECT 'distributors', count(DISTINCT distributor_id), NULL, 'dim_distributor.csv'
FROM dim_distributor
UNION ALL
SELECT 'promotions', count(DISTINCT promo_id), string_agg(DISTINCT mechanic, ', ' ORDER BY mechanic),
       'promotions.csv'
FROM promotions
UNION ALL
SELECT 'documents', count(*), string_agg(source_file, ', ' ORDER BY source_file), 'documents/'
FROM documents
UNION ALL
SELECT 'months', count(DISTINCT month), min(month) || ' to ' || max(month),
       'fact_primary_sales.csv'
FROM sales_by_brand_region_month
UNION ALL
SELECT 'weeks', count(DISTINCT week_start),
       strftime(min(week_start), '%Y-%m-%d') || ' to ' ||
       strftime(max(week_start), '%Y-%m-%d'),
       'fact_primary_sales.csv'
FROM fact_primary_sales
"""


def build_q8(_: Slots) -> tuple[Query, list[str]]:
    """Report what the pack holds, family by family, from the masters themselves."""
    return Query(sql=COVERAGE_SQL, params=[], source_file=PER_ROW_SOURCE), []


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------

Builder = Callable[[Slots], tuple[Query, list[str]]]


@dataclass(frozen=True)
class IntentSpec:
    """One question class: what it answers, what it can narrow by, how it is run."""

    intent: str
    summary: str
    example: str
    filters: frozenset[str]
    dimensions: tuple[str, ...]
    default_dimension: str
    ranks_on: str
    """The measure ``direction`` orders by; empty where the family does not rank."""
    build: Builder | None
    """``None`` for Q7, which has no query of its own and delegates to the actions engine."""


INTENTS: dict[str, IntentSpec] = {
    "Q1": IntentSpec(
        intent="Q1",
        summary="Target versus actual, ranked by the size of the gap.",
        example="Where are we losing most against target this quarter?",
        filters=frozenset(ACHIEVEMENT_FILTERS),
        dimensions=tuple(ACHIEVEMENT_DIMENSIONS),
        default_dimension="brand_region",
        ranks_on="the shortfall against target, in rupees (target minus actual)",
        build=build_q1,
    ),
    "Q2": IntentSpec(
        intent="Q2",
        summary="Primary sales aggregated and ranked over one period.",
        example="Top 5 brands by value in the South in Q3",
        filters=frozenset(SALES_FILTERS),
        dimensions=tuple(SALES_DIMENSIONS),
        default_dimension="brand",
        ranks_on="the chosen metric, value or units",
        build=build_q2,
    ),
    "Q3": IntentSpec(
        intent="Q3",
        summary="The same breakdown compared across two periods.",
        example="How did Beverages in the West move from Q3 to Q4?",
        filters=frozenset(SALES_FILTERS),
        dimensions=tuple(SALES_DIMENSIONS),
        default_dimension="brand",
        ranks_on="the change between the two periods (later minus earlier)",
        build=build_q3,
    ),
    "Q4": IntentSpec(
        intent="Q4",
        summary="Stock-out weeks and days, ranked by exposure.",
        example="Which distributors were worst on Aqualite?",
        filters=frozenset(STOCKOUT_FILTERS),
        dimensions=tuple(STOCKOUT_DIMENSIONS),
        default_dimension="distributor_sku",
        ranks_on="days out of stock",
        build=build_q4,
    ),
    "Q5": IntentSpec(
        intent="Q5",
        summary="Promotion uplift against the four weeks before each promotion.",
        example="Did the Buy 2 Get 1 on the 1L pack work?",
        filters=frozenset(PROMOTION_FILTERS),
        dimensions=(),
        default_dimension="",
        ranks_on="uplift against the four weeks before the promotion",
        build=build_q5,
    ),
    "Q6": IntentSpec(
        intent="Q6",
        summary="Cause and policy read from the tagged documents.",
        example="Why did CremeDelight miss in the North in February?",
        filters=frozenset(DOCUMENT_TAGS),
        dimensions=(),
        default_dimension="",
        ranks_on="",
        build=build_q6,
    ),
    "Q7": IntentSpec(
        intent="Q7",
        summary="What to do next, from the action playbook.",
        example="What should we do about the West?",
        filters=frozenset({"regions"}),
        dimensions=(),
        default_dimension="",
        ranks_on="",
        build=None,
    ),
    "Q8": IntentSpec(
        intent="Q8",
        summary="What the pack covers: entities, periods and counts.",
        example="Which brands and regions are held?",
        filters=frozenset(),
        dimensions=(),
        default_dimension="",
        ranks_on="",
        build=build_q8,
    ),
}


def spec_for(intent: str) -> IntentSpec | None:
    """Return the catalogue entry for *intent*, or ``None`` if it names no known family."""
    return INTENTS.get(intent.strip().upper())
