"""Run the routed intent against the read-only warehouse and shape its evidence rows.

Every figure in a response originates here, in SQL, from the conformed warehouse
(DESIGN.md §3.4, §4.6). Nothing downstream computes a number; the composer may only
restate what these rows already say.

Rows are rounded at this boundary rather than in the prose. What the answer renders has to
be what the evidence holds, or the verifier cannot tell a rounded figure from an invented
one — so rupees are whole here, ratios are fixed to four places, and every ratio also
arrives as the whole-percent figure an answer is actually going to quote.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from acpl_assistant.actions.engine import run_actions
from acpl_assistant.actions.rules import rows
from acpl_assistant.ask.intents import Slots, spec_for
from acpl_assistant.ask.resolve import Refusal

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

    from acpl_assistant.schemas import ActionItem

# --- refusal reasons raised by this stage ----------------------------------

NO_ROUTE = "no_route"
NO_ROWS = "no_rows"

# --- rounding --------------------------------------------------------------

# Rupee columns are whole rupees: the pack's own values carry float noise from the
# roll-up (22002082.560000002), and no answer is going to quote the paise.
INR_SUFFIX = "_inr"

# Ratios keep four places, enough to round-trip a whole percentage point either way.
RATIO_SUFFIX = "_ratio"
RATIO_NAMES = frozenset({"uplift"})
RATIO_DECIMALS = 4

# --- Q7 ---------------------------------------------------------------------

SCOPE_ALL = "all"

# ``/actions`` returns the complete list for a scope and caps nothing, because it is a
# worklist. ``/ask`` returns prose, and prose over forty actions is not an answer.
ACTION_LIMIT = 5

ACTION_FILE = "action_playbook.xlsx"

TERRITORY_REGION_SQL = """
SELECT DISTINCT region
FROM dim_geo
WHERE territory_name IN ({ph}) OR territory_code IN ({ph})
"""

# What each entity family is called when a refusal has to name it.
FAMILY_LABELS: dict[str, str] = {
    "brands": "brand",
    "categories": "category",
    "regions": "region",
    "territories": "territory",
    "distributors": "distributor",
    "skus": "SKU",
    "pack_sizes": "pack size",
    "mechanics": "promotion mechanic",
    "promo_ids": "promotion",
}


@dataclass(frozen=True)
class Execution:
    """The rows an intent produced, or the reason it produced none."""

    intent: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    actions: list[ActionItem] = field(default_factory=list)
    refusal: Refusal | None = None

    @property
    def grounded(self) -> bool:
        """Whether there is anything for a composer to write from."""
        return self.refusal is None and bool(self.rows)


# ---------------------------------------------------------------------------
# Row shaping
# ---------------------------------------------------------------------------


def _is_ratio(column: str) -> bool:
    """Whether a column holds a ratio that an answer will quote as a percentage."""
    return column.endswith(RATIO_SUFFIX) or column in RATIO_NAMES


def shape_row(row: dict[str, Any], source_file: str) -> dict[str, Any]:
    """Round one result row and attach the file it is traceable to.

    A ratio gains a sibling ``*_pct`` column holding the whole-percent figure, because that
    is the form the answer quotes: without it every percentage in the prose would look
    ungrounded against a four-decimal ratio, and the verifier would refuse a correct answer.
    """
    shaped: dict[str, Any] = {"source_file": row.get("source_file") or source_file}
    for column, value in row.items():
        if column == "source_file":
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            shaped[column] = value
            continue
        if _is_ratio(column):
            shaped[column] = round(float(value), RATIO_DECIMALS)
            base = column[: -len(RATIO_SUFFIX)] if column.endswith(RATIO_SUFFIX) else column
            shaped[f"{base}_pct"] = round(float(value) * 100)
        elif column.endswith(INR_SUFFIX):
            shaped[column] = round(float(value))
        else:
            shaped[column] = value
    return shaped


def shape_rows(raw: list[dict[str, Any]], source_file: str) -> list[dict[str, Any]]:
    """Shape every row of a result set."""
    return [shape_row(row, source_file) for row in raw]


# ---------------------------------------------------------------------------
# Q7 — delegation to the actions engine
# ---------------------------------------------------------------------------


def _scope_for(con: DuckDBPyConnection, slots: Slots) -> str:
    """Read the action scope out of the entities: a region, or the whole country.

    A territory is widened to the region that contains it rather than refused. The playbook
    fires on brand × region cells, so Mumbai's actions are the West's actions, and the
    period and region carried on every action say so.
    """
    if slots.entities.regions:
        return slots.entities.regions[0]
    named = slots.entities.territories
    if named:
        placeholders = ", ".join(["?"] * len(named))
        sql = TERRITORY_REGION_SQL.replace("{ph}", placeholders)
        found = rows(con, sql, [*named, *named])
        if found:
            return str(found[0]["region"])
    return SCOPE_ALL


def _run_q7(con: DuckDBPyConnection, slots: Slots) -> Execution:
    """Answer an action question from the playbook engine rather than from a new query."""
    actions = run_actions(con, _scope_for(con, slots))[:ACTION_LIMIT]
    shaped: list[dict[str, Any]] = []
    for action in actions:
        shaped.append(
            {
                "source_file": ACTION_FILE,
                "rule_id": action.rule_id,
                "finding": action.finding,
                "action": action.action,
                "state": action.state,
                "period": action.period,
                "priority": action.priority,
            }
        )
        # The figures behind each finding travel too, so the verifier grounds the numbers
        # in the summary sentence against the rows they were computed from. They are shaped
        # like every other row: ``/actions`` keeps the originals untouched, but two
        # roundings of one rupee figure inside one response would read as two figures.
        shaped.extend(shape_row(row.model_dump(), ACTION_FILE) for row in action.evidence)
    return Execution(intent="Q7", rows=shaped, actions=actions)


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------


def _no_route(intent: str, unhonoured: list[str]) -> Refusal:
    """Refuse a question whose intent cannot narrow by something the question named."""
    labels = sorted({FAMILY_LABELS.get(family, family) for family in unhonoured})
    named = ", ".join(labels)
    return Refusal(
        reason=NO_ROUTE,
        message=(
            f"This question names a {named}, and the figures it asks for are not recorded "
            f"at that level. Answering would report a wider total than the question asked "
            f"for, so no figure is given. ({intent})"
        ),
    )


def _no_rows(slots: Slots) -> Refusal:
    """State, as fact, that the warehouse holds nothing matching the question."""
    named = ", ".join(value for values in slots.entities.as_dict().values() for value in values)
    subject = f" for {named}" if named else ""
    return Refusal(
        reason=NO_ROWS,
        message=(
            f"The data pack records nothing{subject} in {slots.period.label}. "
            "No figure is reported rather than one inferred from a wider period."
        ),
    )


def execute(con: DuckDBPyConnection, intent: str, slots: Slots) -> Execution:
    """Run *intent* over *slots* and return its evidence rows, or the reason there are none.

    Raises nothing on a question this system cannot answer: an unknown intent, a filter the
    intent cannot honour and an empty result are all refusals with a reason, which is what
    lets ``/ask`` answer every request without ever returning a 500.
    """
    spec = spec_for(intent)
    if spec is None:
        return Execution(
            intent=intent,
            refusal=Refusal(
                reason=NO_ROUTE,
                message=(
                    "This question does not map to anything the data pack can answer. It "
                    "covers primary sales, targets, stock-outs, promotions and the FY26 "
                    "documents; there is no general query path beyond those."
                ),
            ),
        )

    if spec.build is None:
        return _run_q7(con, slots)

    query, unhonoured = spec.build(slots)
    if unhonoured:
        return Execution(intent=spec.intent, refusal=_no_route(spec.intent, unhonoured))

    shaped = shape_rows(rows(con, query.sql, query.params), query.source_file)
    if not shaped:
        return Execution(intent=spec.intent, refusal=_no_rows(slots))
    return Execution(intent=spec.intent, rows=shaped)
