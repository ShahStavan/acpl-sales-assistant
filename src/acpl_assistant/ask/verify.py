"""Check a drafted answer against the rows it was supposed to be written from.

Two checks run after composition and before anything is returned (DESIGN.md §3.3):

* **Numeric grounding.** Every numeral in the prose must be traceable to the evidence, the
  resolved period, or something the question itself named. A figure that is not becomes
  ``ungrounded_figure`` and the answer is withheld.
* **Premise.** Where the question asserted a direction of travel and the rows can settle it,
  a contradicted assertion becomes ``false_premise`` — the fifth refusal class, and the one
  that cannot be decided before the figures exist.

Grounding is checked structurally rather than asked for. A model told to copy figures will
mostly copy figures; the point of this module is that "mostly" is not a property anyone can
publish an accuracy number against.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from acpl_assistant.ask.intents import Slots

# --- refusal reasons raised by this stage ----------------------------------

UNGROUNDED_FIGURE = "ungrounded_figure"
FALSE_PREMISE = "false_premise"

# A run of digits, with thousands separators and decimals if they are written. Nothing is
# excluded by what precedes it: "D032", "FY26" and "Q3" all yield their digits, and the
# same extractor runs over the evidence, so both sides tokenise identically.
_NUMERAL = re.compile(r"\d[\d,]*(?:\.\d+)?")

# An ordered-list marker at the start of a line. "1." before a brand name is layout, not a
# figure, and treating it as one would refuse correct rankings for being ranked.
_LIST_MARKER = re.compile(r"^[ \t]*\d+[.)](?=\s)", re.MULTILINE)

# Columns whose sign settles an asserted direction of travel.
_DELTA_PREFIX = "delta_"
_CHANGE_RATIO = "change_ratio"
_ACHIEVEMENT_RATIO = "achievement_ratio"

# Achievement at or above this is a beat, below it a miss.
_ON_TARGET = 1.0


@dataclass(frozen=True)
class Verdict:
    """Whether the drafted answer may be returned, and why not if it may not."""

    ok: bool
    reason: str = ""
    message: str = ""
    ungrounded: tuple[str, ...] = field(default_factory=tuple)
    """The numerals that no evidence row supports, in the order they were written."""


# ---------------------------------------------------------------------------
# Numeric grounding
# ---------------------------------------------------------------------------


def _as_number(token: str) -> float | None:
    """Read a written numeral, ignoring thousands separators."""
    try:
        return float(token.replace(",", ""))
    except ValueError:  # pragma: no cover - the pattern only matches parseable runs
        return None


def _decimals(token: str) -> int:
    """How many decimal places a numeral was written to."""
    _, _, fraction = token.replace(",", "").partition(".")
    return len(fraction)


def _numerals(text: str) -> list[str]:
    """Every numeral in *text*, list markers removed first."""
    return _NUMERAL.findall(_LIST_MARKER.sub("", text))


def _ground_value(value: Any, into: set[float]) -> None:
    """Add whatever numbers *value* carries to the grounded set.

    Numbers go in directly; strings are re-read with the same extractor, so a date, an
    identifier and a rupee figure inside a finding sentence all ground the digits a reader
    would see in them.
    """
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        into.add(float(value))
        return
    for token in _numerals(str(value)):
        number = _as_number(token)
        if number is not None:
            into.add(number)


def grounded_values(
    rows: Sequence[dict[str, Any]], slots: Slots | None = None, extra: Iterable[Any] = ()
) -> set[float]:
    """Every number an answer is entitled to state.

    The evidence rows are the substance of it. The period, the months it spans and the
    entities the question named are added because an answer has to be able to say *when*
    and *what* — "FY26 Q3" and "1L" are not figures the model invented.
    """
    values: set[float] = set()
    for row in rows:
        for value in row.values():
            _ground_value(value, values)
    if slots is not None:
        for period in (slots.period, slots.compare_to):
            if period is None:
                continue
            _ground_value(period.label, values)
            for month in period.months:
                _ground_value(month, values)
        for named in slots.entities.as_dict().values():
            for value in named:
                _ground_value(value, values)
        values.add(float(slots.top_n))
    values.add(float(len(rows)))
    for value in extra:
        _ground_value(value, values)
    return values


def _supported(token: str, values: set[float]) -> bool:
    """Whether one written numeral matches something in the grounded set.

    A figure written to fewer decimal places than it was computed to still matches, so an
    answer may say 93 for 92.77. It may not say 2.2 for 22002083: rounding a figure is
    restating it, but rescaling one is arithmetic, and arithmetic belongs in SQL.
    """
    number = _as_number(token)
    if number is None:  # pragma: no cover - the pattern only matches parseable runs
        return False
    places = _decimals(token)
    return any(round(value, places) == number for value in values)


def ungrounded_figures(answer: str, values: set[float]) -> list[str]:
    """Every numeral in *answer* that nothing in the grounded set supports."""
    seen: list[str] = []
    for token in _numerals(answer):
        if not _supported(token, values) and token not in seen:
            seen.append(token)
    return seen


# ---------------------------------------------------------------------------
# Premise
# ---------------------------------------------------------------------------


def _direction_of(rows: Sequence[dict[str, Any]]) -> float | None:
    """The net change the rows show, or ``None`` where they show no change at all.

    Read from the top row rather than summed over all of them: the ranking's leading row is
    the one the question is about, and summing a ranking of changes would let two entities
    moving opposite ways cancel into a false "flat".
    """
    if not rows:
        return None
    row = rows[0]
    for column, value in row.items():
        if column.startswith(_DELTA_PREFIX) and isinstance(value, (int, float)):
            return float(value)
    change = row.get(_CHANGE_RATIO)
    if isinstance(change, (int, float)):
        return float(change)
    return None


def _achievement_of(rows: Sequence[dict[str, Any]]) -> float | None:
    """The achievement ratio the leading row shows, if it carries one."""
    if not rows:
        return None
    value = rows[0].get(_ACHIEVEMENT_RATIO)
    return float(value) if isinstance(value, (int, float)) else None


def check_premise(premise: str, rows: Sequence[dict[str, Any]]) -> str | None:
    """Return a correction where the rows contradict what the question assumed.

    Silent where the rows cannot settle it. A question asserting growth that was routed to a
    single-period ranking has no change column to be wrong about, and refusing it for want
    of evidence either way would turn a missing check into a missing answer.
    """
    if premise in {"growth", "decline"}:
        movement = _direction_of(rows)
        if movement is None or movement == 0:
            return None
        if premise == "growth" and movement < 0:
            return "it fell over the period rather than growing"
        if premise == "decline" and movement > 0:
            return "it grew over the period rather than declining"
        return None

    if premise in {"miss", "beat"}:
        achievement = _achievement_of(rows)
        if achievement is None:
            return None
        if premise == "miss" and achievement >= _ON_TARGET:
            return "it met or beat its target over the period rather than missing"
        if premise == "beat" and achievement < _ON_TARGET:
            return "it missed its target over the period rather than beating it"
    return None


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------


def verify(
    answer: str, rows: Sequence[dict[str, Any]], slots: Slots, premise: str = "none"
) -> Verdict:
    """Check the drafted answer, and say which check it failed.

    The premise check runs first: an answer built on an assumption the data contradicts is
    wrong even when every figure in it is real, and saying so is more useful than reporting
    a figure against a question that should not have been asked that way.
    """
    correction = check_premise(premise, rows)
    if correction is not None:
        return Verdict(
            ok=False,
            reason=FALSE_PREMISE,
            message=(
                f"That question assumes something the data does not show: {correction}. "
                "No figure is reported against the assumption as stated."
            ),
        )

    missing = ungrounded_figures(answer, grounded_values(rows, slots))
    if missing:
        return Verdict(
            ok=False,
            reason=UNGROUNDED_FIGURE,
            message=(
                "The drafted answer stated a figure that no source row supports "
                f"({', '.join(missing)}), so it was withheld. Every number this service "
                "reports is computed in SQL from the provided files."
            ),
            ungrounded=tuple(missing),
        )
    return Verdict(ok=True)
