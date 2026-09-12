"""Scope resolution, grouping by target entity, ranking by recency and INR at risk.

Unresolvable scope returns ``[]``. Nothing is executed in either state. DESIGN.md §5.4–§5.6.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import datetime as dt
from typing import TYPE_CHECKING, Any

from acpl_assistant.actions import rules
from acpl_assistant.schemas import ActionItem, ActionState, evidence_from_rows

if TYPE_CHECKING:
    from collections.abc import Sequence

    from duckdb import DuckDBPyConnection

# The scope that means "every region".
SCOPE_ALL = "all"

# Suffixes a caller may append to a region name.  Stripped before matching, so
# "west region" and "West India" both resolve to West.
SCOPE_SUFFIXES = ("region", "india", "zone")

REGIONS_SQL = "SELECT DISTINCT region FROM dim_geo ORDER BY region"


@dataclass(frozen=True)
class PlaybookEntry:
    """One row of ``action_playbook.xlsx`` as the warehouse holds it."""

    rule_id: str
    raw_condition: str
    action: str
    needs_approval: bool

    @property
    def state(self) -> ActionState:
        """Approval state, read from the playbook rather than inferred (DESIGN.md §5.6)."""
        return "PENDING_APPROVAL" if self.needs_approval else "RECOMMENDED"


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def _normalise_scope(scope: str) -> str:
    """Casefold, collapse whitespace and drop a trailing region word."""
    words = scope.strip().casefold().split()
    while words and words[-1] in SCOPE_SUFFIXES:
        words.pop()
    return " ".join(words)


def resolve_scope(con: DuckDBPyConnection, scope: str) -> str | None:
    """Resolve *scope* to a region name or ``"all"``; return ``None`` if it resolves to neither.

    Matching is case-insensitive and tolerates a trailing "region", and is otherwise exact.
    There is deliberately no fuzzy matching here: resolving "Wets" to West would hand a
    manager the actions for a region they did not ask about, and a withheld ``[]`` is the
    safer failure. Fuzzy entity resolution belongs on the ``/ask`` path, where a near miss
    is caught by the refusal classes.
    """
    normalised = _normalise_scope(scope)
    if not normalised:
        return None
    if normalised == SCOPE_ALL:
        return SCOPE_ALL
    regions = {row["region"].casefold(): row["region"] for row in rules.rows(con, REGIONS_SQL)}
    return regions.get(normalised)


# ---------------------------------------------------------------------------
# Playbook
# ---------------------------------------------------------------------------


def load_playbook(con: DuckDBPyConnection) -> dict[str, PlaybookEntry]:
    """Read the playbook from the warehouse, keyed by rule id.

    The action wording and the approval flag both come from here, so the response quotes
    ACPL's own playbook and editing the spreadsheet changes behaviour rather than leaving
    the code to drift away from it.
    """
    return {
        row["rule_id"]: PlaybookEntry(
            rule_id=row["rule_id"],
            raw_condition=row["raw_condition"],
            action=row["action"],
            needs_approval=bool(row["needs_approval"]),
        )
        for row in rules.rows(con, rules.PLAYBOOK_SQL)
    }


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def _merge_period(group: Sequence[rules.Finding]) -> str:
    """Render one period label covering every finding in the group.

    Month-grain rules report months, so a group spanning April to June reads
    ``2026-04..2026-06``; week-grain rules keep the ISO dates their rows carry.
    """
    if all(f.month_grain for f in group):
        months = sorted(f.period_label for f in group)
        return months[0] if months[0] == months[-1] else f"{months[0]}..{months[-1]}"
    start = min(f.period_start for f in group)
    end = max(f.period_end for f in group)
    return f"{start.isoformat()}..{end.isoformat()}"


def _merge_evidence(group: Sequence[rules.Finding]) -> list[dict[str, Any]]:
    """Concatenate the group's evidence, dropping rows repeated across findings."""
    seen: set[tuple] = set()
    merged: list[dict[str, Any]] = []
    for finding in group:
        for row in finding.evidence:
            key = tuple(sorted((k, str(v)) for k, v in row.items()))
            if key in seen:
                continue
            seen.add(key)
            merged.append(row)
    return merged


def _merge_value_inr(group: Sequence[rules.Finding]) -> float | None:
    """Sum the rupee magnitudes, or ``None`` where the rule has no rupee figure at all."""
    values = [f.value_inr for f in group if f.value_inr is not None]
    return sum(values) if values else None


@dataclass(frozen=True)
class GroupedFinding:
    """One rule against one entity, merged across every period it fired in."""

    rule_id: str
    entity: tuple[str, ...]
    entity_label: str
    region: str
    period: str
    recency: dt.date
    figures: dict[str, Any]
    evidence: list[dict[str, Any]]
    value_inr: float | None
    magnitude: float


def group_findings(findings: Sequence[rules.Finding]) -> list[GroupedFinding]:
    """Collapse findings to one per (rule, entity), the entity the action targets.

    R-07 and R-08 fire often — 23 promotions and 47 distributor-months on this data — and
    a list of 47 separate calls to 27 distributors is not the action the playbook
    prescribes. Grouping is what turns findings into decisions (DESIGN.md §5.4).
    """
    buckets: dict[tuple[str, tuple[str, ...]], list[rules.Finding]] = defaultdict(list)
    for finding in findings:
        buckets[(finding.rule_id, finding.entity)].append(finding)

    grouped = []
    for (rule_id, entity), bucket in buckets.items():
        group = sorted(bucket, key=lambda f: (f.period_start, f.period_label))
        grouped.append(
            GroupedFinding(
                rule_id=rule_id,
                entity=entity,
                entity_label=group[0].entity_label,
                region=group[0].region,
                period=_merge_period(group),
                recency=max(f.period_end for f in group),
                figures=rules.RULES[rule_id].aggregate(group),
                evidence=_merge_evidence(group),
                value_inr=_merge_value_inr(group),
                magnitude=max(f.magnitude for f in group),
            )
        )
    return grouped


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def rank_key(item: GroupedFinding) -> tuple:
    """Sort key: most recent first, then most rupees at risk, then rule severity.

    The last two components are not tie-breakers anyone reads; they are there so the
    order is total. An identical warehouse must produce an identical list, or the
    published figures could not be tested.
    """
    has_value = 0 if item.value_inr is not None else 1
    return (
        -item.recency.toordinal(),
        has_value,
        -(item.value_inr or 0.0),
        -item.magnitude,
        item.rule_id,
        item.entity,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_actions(con: DuckDBPyConnection, scope: str) -> list[ActionItem]:
    """Evaluate every playbook rule over FY26 and return the actions for *scope*.

    Returns ``[]`` for a scope that resolves to no region, and for a scope that resolves
    but has nothing to report. Nothing is capped: the list is complete for the scope.
    """
    resolved = resolve_scope(con, scope)
    if resolved is None:
        return []

    playbook = load_playbook(con)
    everything = resolved == SCOPE_ALL
    findings = [f for f in rules.evaluate_all(con) if everything or f.region == resolved]

    ranked = sorted(group_findings(findings), key=rank_key)
    return [
        ActionItem(
            finding=rules.RULES[item.rule_id].summarise(
                item.entity_label, item.period, item.figures
            ),
            rule_id=item.rule_id,
            action=playbook[item.rule_id].action,
            state=playbook[item.rule_id].state,
            period=item.period,
            evidence=evidence_from_rows(item.evidence),
            priority=position,
        )
        for position, item in enumerate(ranked, start=1)
    ]
