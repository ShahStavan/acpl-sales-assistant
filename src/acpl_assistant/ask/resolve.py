"""Entity and period resolution against controlled vocabularies built from the masters.

Handles fuzzy matching (rapidfuzz), fiscal-quarter arithmetic and relative-time anchoring to
the latest data week (2026-06-23). Unknown entity, out-of-period and unsupported-metric
refusals originate here. DESIGN.md §2.4, §2.5.

Nothing in this module calls a model. Every refusal it produces is therefore free, decided
before a token is spent, and reproducible from the warehouse alone.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
import datetime as dt
import re
from typing import TYPE_CHECKING

from rapidfuzz import fuzz, process

from acpl_assistant.actions.rules import rows

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from duckdb import DuckDBPyConnection

# ---------------------------------------------------------------------------
# Temporal frame — DESIGN.md §2.5
# ---------------------------------------------------------------------------

# Relative references anchor here, not to the wall clock. Clock-anchored resolution would
# make the brief's own example question unanswerable the moment the data period passed.
DATA_LATEST_WEEK = dt.date(2026, 6, 23)

FY_START_MONTH = 7
FY26_FIRST_MONTH = "2025-07"
FY26_LAST_MONTH = "2026-06"
FY26_LABEL = "FY26 (July 2025 – June 2026)"

# Above this, a year reference is written in full (2026) rather than as FY shorthand (26).
FOUR_DIGIT_YEAR = 1000

# Fiscal quarters: Q1 = Jul–Sep, Q2 = Oct–Dec, Q3 = Jan–Mar, Q4 = Apr–Jun.
FISCAL_QUARTERS: dict[int, tuple[str, ...]] = {
    1: ("2025-07", "2025-08", "2025-09"),
    2: ("2025-10", "2025-11", "2025-12"),
    3: ("2026-01", "2026-02", "2026-03"),
    4: ("2026-04", "2026-05", "2026-06"),
}

# The quarter and month the anchor week falls in, which is what "this" refers to.
CURRENT_QUARTER = 4
CURRENT_MONTH = "2026-06"
PREVIOUS_MONTH = "2026-05"
PREVIOUS_QUARTER = 3

ALL_MONTHS: tuple[str, ...] = tuple(m for q in sorted(FISCAL_QUARTERS) for m in FISCAL_QUARTERS[q])

# ---------------------------------------------------------------------------
# Refusal reasons — stable tokens, asserted by the evaluation set
# ---------------------------------------------------------------------------

UNKNOWN_ENTITY = "unknown_entity"
OUT_OF_PERIOD = "out_of_period"
UNSUPPORTED_METRIC = "unsupported_metric"

# ---------------------------------------------------------------------------
# Unsupported metrics — DESIGN.md §2.4
# ---------------------------------------------------------------------------

# None of these exist anywhere in the pack. The pack holds primary sales (value and units),
# targets, stock-out days, and promotion windows — nothing else. A question asking for one
# of these is refused with the reason, not answered from a proxy.
UNSUPPORTED_METRICS: dict[str, str] = {
    "secondary sales": "secondary sales",
    "sell-out": "sell-out (secondary) sales",
    "sell out": "sell-out (secondary) sales",
    "sellout": "sell-out (secondary) sales",
    "offtake": "retail offtake",
    "margin": "margin",
    "margins": "margin",
    "gross margin": "margin",
    "profit": "profit",
    "profitable": "profit",
    "profitability": "profitability",
    "net profit": "profit",
    "market share": "market share",
    "share of market": "market share",
    "competitor": "competitor figures",
    "competitors": "competitor figures",
    "competition volume": "competitor figures",
    "roi": "promotion ROI",
    "return on investment": "promotion ROI",
    "inventory value": "inventory valuation",
    "stock value": "inventory valuation",
    "footfall": "retail footfall",
    "forecast": "a forecast",
    "forecasts": "a forecast",
    "predict": "a forecast",
    "projection": "a forecast",
    "next quarter": "a forecast",
    "next month": "a forecast",
    "next year": "a forecast",
}

# What the pack does hold, quoted back so a refusal is useful rather than merely correct.
SUPPORTED_METRICS = (
    "primary sales value and units, targets and achievement, stock-out days, and promotion windows"
)

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

# rapidfuzz score above which a typo is treated as the same entity. Compared on whole
# normalised strings with ``fuzz.ratio`` — never ``WRatio``, whose partial-ratio component
# matches any substring and will happily read "losing most against target" as "Home Care".
FUZZY_ACCEPT = 90

# Shorter than this, a fuzzy comparison is noise: "South" and "1.5L" are four or five
# characters and score against half the vocabulary.
FUZZY_MIN_CHARS = 6

VOCAB_SQL: dict[str, str] = {
    "brands": "SELECT DISTINCT brand AS v FROM dim_sku ORDER BY 1",
    "categories": "SELECT DISTINCT category AS v FROM dim_sku ORDER BY 1",
    "regions": "SELECT DISTINCT region AS v FROM dim_geo ORDER BY 1",
    "territories": "SELECT DISTINCT territory_name AS v FROM dim_geo ORDER BY 1",
    "territory_codes": "SELECT DISTINCT territory_code AS v FROM dim_geo ORDER BY 1",
    "distributor_ids": "SELECT DISTINCT distributor_id AS v FROM dim_distributor ORDER BY 1",
    "distributor_names": "SELECT DISTINCT distributor_name AS v FROM dim_distributor ORDER BY 1",
    "sku_codes": "SELECT DISTINCT sku_code AS v FROM dim_sku ORDER BY 1",
    "sku_names": "SELECT DISTINCT sku_name AS v FROM dim_sku ORDER BY 1",
    "pack_sizes": "SELECT DISTINCT pack_size AS v FROM dim_sku ORDER BY 1",
    "mechanics": "SELECT DISTINCT mechanic AS v FROM promotions ORDER BY 1",
    "promo_ids": "SELECT DISTINCT promo_id AS v FROM promotions ORDER BY 1",
}

# Families a bare phrase may name. Ordered so the more specific family claims a phrase
# first: "Aloe Vera Glow 100ml" is a SKU, and only its prefix is the brand.
SCAN_FAMILIES: tuple[tuple[str, str], ...] = (
    ("sku_names", "skus"),
    ("distributor_names", "distributors"),
    ("territories", "territories"),
    ("brands", "brands"),
    ("categories", "categories"),
    ("mechanics", "mechanics"),
    ("pack_sizes", "pack_sizes"),
    ("regions", "regions"),
)


def normalise(text: str) -> str:
    """Strip a name down to the letters and digits in it, casefolded.

    "Aqua Lite", "aqualite" and "AQUALITE" all reduce to ``aqualite``, so ordinary spacing
    and punctuation differences resolve without any fuzzy matching at all.
    """
    return re.sub(r"[^a-z0-9]", "", text.casefold())


@dataclass(frozen=True)
class Vocabulary:
    """Every value the warehouse holds for each entity family, read once.

    Built from the masters rather than hard-coded, so a changed pack changes what the
    system will accept without anyone editing a list by hand.
    """

    values: dict[str, tuple[str, ...]]
    index: dict[str, dict[str, str]]
    """Per family, normalised form → canonical value, for exact lookup."""

    def of(self, family: str) -> tuple[str, ...]:
        """Return the values held for *family*."""
        return self.values.get(family, ())

    def match(self, family: str, text: str) -> str | None:
        """Return the canonical value in *family* that *text* names, or ``None``.

        Exact on the normalised form first. Only then a whole-string ``fuzz.ratio`` pass,
        gated on length, to absorb a genuine typo — "Aqualight" for Aqualite. Partial and
        token-set scorers are deliberately not used: they match any phrase that merely
        contains a vocabulary word, which reads half an English sentence as an entity.
        """
        needle = normalise(text)
        if not needle:
            return None
        exact = self.index.get(family, {}).get(needle)
        if exact is not None:
            return exact
        if len(needle) < FUZZY_MIN_CHARS:
            return None
        hit = process.extractOne(
            needle,
            self.index.get(family, {}).keys(),
            scorer=fuzz.ratio,
            score_cutoff=FUZZY_ACCEPT,
        )
        return self.index[family][hit[0]] if hit else None

    def knows(self, text: str) -> bool:
        """Whether *text* names something in any vocabulary at all.

        Asked through :meth:`match` rather than by exact lookup, so this agrees with the
        scanner by construction. An exact test would report "Aqalite" as an unknown entity
        in the same breath as the scanner resolved it to Aqualite, and the question would
        be refused for naming the brand it had just been understood to name.
        """
        return any(self.match(family, text) is not None for family in self.index)


def build_vocabulary(con: DuckDBPyConnection) -> Vocabulary:
    """Read every controlled vocabulary out of the warehouse."""
    values = {
        family: tuple(str(r["v"]) for r in rows(con, sql) if r["v"] is not None)
        for family, sql in VOCAB_SQL.items()
    }
    index = {
        family: {normalise(v): v for v in members if normalise(v)}
        for family, members in values.items()
    }
    return Vocabulary(values=values, index=index)


# ---------------------------------------------------------------------------
# Period parsing
# ---------------------------------------------------------------------------

_MONTH_NUMBERS: dict[str, int] = {}
for _i in range(1, 13):
    _MONTH_NUMBERS[calendar.month_name[_i].casefold()] = _i
    _MONTH_NUMBERS[calendar.month_abbr[_i].casefold()] = _i

_MONTH_WORDS = "|".join(sorted(_MONTH_NUMBERS, key=len, reverse=True))

_ISO_MONTH = re.compile(r"\b(20\d\d)-(0[1-9]|1[0-2])\b")
_MONTH_YEAR = re.compile(rf"\b({_MONTH_WORDS})\b[\s,]*((?:19|20)\d\d)?", re.IGNORECASE)
_QUARTER = re.compile(r"\bq([1-4])\b(?:\s*(?:of\s*)?(fy\s*\d{2,4}|20\d\d))?", re.IGNORECASE)
_FY = re.compile(r"\bfy\s*-?\s*(\d{2,4})\b", re.IGNORECASE)
_BARE_YEAR = re.compile(r"\b(19|20)\d\d\b")
_HALF = re.compile(r"\bh([12])\b(?:\s*fy\s*\d{2,4})?", re.IGNORECASE)


@dataclass(frozen=True)
class Period:
    """A resolved stretch of FY26, as the months it covers."""

    label: str
    months: tuple[str, ...]

    @property
    def start_month(self) -> str:
        """First month in the period, ``YYYY-MM``."""
        return self.months[0]

    @property
    def end_month(self) -> str:
        """Last month in the period, ``YYYY-MM``."""
        return self.months[-1]

    @property
    def start_date(self) -> dt.date:
        """First day of the first month."""
        return dt.date.fromisoformat(f"{self.start_month}-01")

    @property
    def end_date(self) -> dt.date:
        """Last day of the last month."""
        year, month = (int(p) for p in self.end_month.split("-"))
        return dt.date(year, month, calendar.monthrange(year, month)[1])


FULL_YEAR = Period(label="FY26", months=ALL_MONTHS)


def _fy_number(raw: str) -> int:
    """Normalise ``26``, ``2026`` or ``fy26`` to the fiscal-year number 26."""
    digits = re.sub(r"\D", "", raw)
    value = int(digits)
    return value % 100 if value >= FOUR_DIGIT_YEAR else value


def _month_key(month_word: str, year: int) -> str:
    """Render ``("February", 2026)`` as ``2026-02``."""
    return f"{year:04d}-{_MONTH_NUMBERS[month_word.casefold()]:02d}"


def _fy_year_for_month(month_number: int) -> int:
    """Which calendar year a month belongs to inside FY26.

    July to December are 2025, January to June are 2026 — so "February" with no year is
    unambiguous within the fiscal year and does not need one.
    """
    return 2025 if month_number >= FY_START_MONTH else 2026


@dataclass(frozen=True)
class PeriodParse:
    """The outcome of reading a period out of a question."""

    period: Period | None = None
    out_of_period: str = ""
    """What was named that FY26 does not cover, ready to quote in the refusal."""


def parse_period(question: str) -> PeriodParse:  # noqa: PLR0911
    """Read the period a question refers to, anchored to the latest data week.

    Returns the full fiscal year when nothing temporal is named: a question with no period
    is a question about the data that exists, not an error.
    """
    text = question.casefold()

    # --- explicit out-of-range years, checked before anything is accepted ---
    for match in _ISO_MONTH.finditer(question):
        key = f"{match.group(1)}-{match.group(2)}"
        if key not in ALL_MONTHS:
            return PeriodParse(out_of_period=key)
        return PeriodParse(period=Period(label=key, months=(key,)))

    fy = _FY.search(question)
    if fy and _fy_number(fy.group(1)) != 26:  # noqa: PLR2004
        return PeriodParse(out_of_period=f"FY{_fy_number(fy.group(1)):02d}")

    # --- fiscal quarters ----------------------------------------------------
    quarter = _QUARTER.search(question)
    if quarter:
        qualifier = quarter.group(2)
        if (
            qualifier
            and not qualifier.lower().startswith("fy")
            and int(qualifier)
            not in (
                2025,
                2026,
            )
        ):
            return PeriodParse(out_of_period=qualifier)
        if qualifier and qualifier.lower().startswith("fy") and _fy_number(qualifier) != 26:  # noqa: PLR2004
            return PeriodParse(out_of_period=f"FY{_fy_number(qualifier):02d}")
        number = int(quarter.group(1))
        return PeriodParse(period=Period(label=f"FY26 Q{number}", months=FISCAL_QUARTERS[number]))

    # --- halves -------------------------------------------------------------
    half = _HALF.search(question)
    if half:
        number = int(half.group(1))
        months = ALL_MONTHS[:6] if number == 1 else ALL_MONTHS[6:]
        return PeriodParse(period=Period(label=f"FY26 H{number}", months=months))

    # --- named months -------------------------------------------------------
    named = _MONTH_YEAR.search(question)
    if named:
        word = named.group(1)
        number = _MONTH_NUMBERS[word.casefold()]
        year = int(named.group(2)) if named.group(2) else _fy_year_for_month(number)
        key = f"{year:04d}-{number:02d}"
        if key not in ALL_MONTHS:
            return PeriodParse(out_of_period=f"{word.title()} {year}")
        return PeriodParse(period=Period(label=key, months=(key,)))

    # --- relative references, anchored to the data --------------------------
    if "this quarter" in text or "current quarter" in text:
        return PeriodParse(
            period=Period(label=f"FY26 Q{CURRENT_QUARTER}", months=FISCAL_QUARTERS[CURRENT_QUARTER])
        )
    if "last quarter" in text or "previous quarter" in text:
        return PeriodParse(
            period=Period(
                label=f"FY26 Q{PREVIOUS_QUARTER}", months=FISCAL_QUARTERS[PREVIOUS_QUARTER]
            )
        )
    if "this month" in text or "current month" in text:
        return PeriodParse(period=Period(label=CURRENT_MONTH, months=(CURRENT_MONTH,)))
    if "last month" in text or "previous month" in text:
        return PeriodParse(period=Period(label=PREVIOUS_MONTH, months=(PREVIOUS_MONTH,)))
    if "this year" in text or "this fy" in text or "full year" in text or "fy26" in text:
        return PeriodParse(period=FULL_YEAR)

    # --- a bare year that is not one FY26 touches ---------------------------
    year_match = _BARE_YEAR.search(question)
    if year_match and int(year_match.group(0)) not in (2025, 2026):
        return PeriodParse(out_of_period=year_match.group(0))

    return PeriodParse(period=FULL_YEAR)


def parse_periods(question: str) -> list[Period]:
    """Return every in-range period the question names, in the order it names them.

    Q3 compares two stretches of the year — "how did Beverages in the West move from Q3 to
    Q4" — and :func:`parse_period` deliberately reports only the first. This reads all of
    them, so a comparison has both sides. An out-of-range reference is dropped here rather
    than refused; :func:`parse_period` has already refused the question if the *first*
    period named is outside FY26.
    """
    found: list[Period] = []

    def add(period: Period) -> None:
        if all(p.label != period.label for p in found):
            found.append(period)

    for match in _QUARTER.finditer(question):
        add(Period(label=f"FY26 Q{match.group(1)}", months=FISCAL_QUARTERS[int(match.group(1))]))
    for match in _ISO_MONTH.finditer(question):
        key = f"{match.group(1)}-{match.group(2)}"
        if key in ALL_MONTHS:
            add(Period(label=key, months=(key,)))
    for match in _MONTH_YEAR.finditer(question):
        word = match.group(1)
        number = _MONTH_NUMBERS[word.casefold()]
        year = int(match.group(2)) if match.group(2) else _fy_year_for_month(number)
        key = f"{year:04d}-{number:02d}"
        if key in ALL_MONTHS:
            add(Period(label=key, months=(key,)))
    return found


# ---------------------------------------------------------------------------
# Entity scanning
# ---------------------------------------------------------------------------

# Words that look like entities to a naive scanner but are ordinary English, plus the
# vocabulary of the question domain itself.
STOPWORDS = frozenset(
    # SIM905 would have this as a list literal.  A wrapped block of prose is how a
    # stopword list stays reviewable: a word added or removed shows up as a one-word
    # diff rather than a reflowed 40-line literal.
    """
    a an the and or but if then than that this these those what which who whom whose when
    where why how much many is are was were be been being do does did done have has had
    our we us my me your you they them their it its of in on at to for from by with
    about against between into during before after above below over under again further
    show tell give list rank top bottom best worst most least more less highest lowest
    compare comparison versus vs change moved move movement trend performance performing
    sales sale target targets achievement gap shortfall miss missed missing below behind
    stock stockout stockouts out promotion promotions promo promos uplift discount mechanic
    region regions territory territories distributor distributors brand brands sku skus
    category categories pack month months week weeks quarter quarters year years value
    units inr rupees crore lakh percent percentage should action actions recommend
    q1 q2 q3 q4 fy fy26 h1 h2 all any each per week-on-week yoy
    """.split()  # noqa: SIM905
)

# Capitalised words that are ordinary English or units, never an ACPL entity.
NON_ENTITY_PROPER = frozenset(
    """
    india indian inr rupee rupees crore lakh monday tuesday wednesday thursday friday
    saturday sunday fy fy26 sku skus mrp ncr acpl sop hr
    """.split()  # noqa: SIM905
)

# Longest alternative first: "PR-2026-005" must not be consumed as "PR-2026" by the
# shorter SKU-code branch, which would leave a promotion id that matches no promotion.
_CODE = re.compile(r"\b(?:PR-\d{4}-\d{3}|D\d{3}|[A-Z]{1,3}-?\d{3,4})\b", re.IGNORECASE)

# A capitalised word mid-sentence that no vocabulary knows is a proper noun the pack does
# not hold — which is exactly the unknown-entity refusal. Sentence-initial words are
# excluded because English capitalises them regardless.
_PROPER_NOUN = re.compile(r"(?<![.!?]\s)(?<!^)\b([A-Z][A-Za-z&'\-]{2,})\b", re.MULTILINE)


@dataclass
class Entities:
    """Everything the question named that the warehouse holds."""

    brands: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    territories: list[str] = field(default_factory=list)
    distributors: list[str] = field(default_factory=list)
    skus: list[str] = field(default_factory=list)
    pack_sizes: list[str] = field(default_factory=list)
    mechanics: list[str] = field(default_factory=list)
    promo_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, list[str]]:
        """Return the named entities as a plain dict, empty families dropped."""
        return {
            name: values
            for name, values in vars(self).items()
            if isinstance(values, list) and values
        }

    @property
    def any_named(self) -> bool:
        """Whether the question named anything at all."""
        return bool(self.as_dict())


def _phrases(question: str, max_words: int = 4) -> list[tuple[str, int, int]]:
    """Every contiguous run of up to *max_words* words, longest first.

    Multi-word names — "Aloe Vera Glow", "Namkeen Nation", "Delhi NCR", "Home Care" — only
    match if the scanner offers the whole phrase. Each run carries the word positions it
    spans so a match can claim them and stop its own words being read again.
    """
    words = re.findall(r"[A-Za-z0-9&'\-\.]+", question)
    out: list[tuple[str, int, int]] = []
    for size in range(max_words, 0, -1):
        out.extend(
            (" ".join(words[i : i + size]), i, i + size) for i in range(len(words) - size + 1)
        )
    return out


def scan_entities(question: str, vocab: Vocabulary) -> tuple[Entities, list[str]]:
    """Find every vocabulary value the question names, and every proper noun it does not.

    The second return value carries capitalised words that no vocabulary knows. Those are
    what an unknown-entity refusal is made of: the pack holds 15 brands, and a question
    about a sixteenth has no honest answer.
    """
    found = Entities()
    taken: set[int] = set()

    for phrase, start_i, end_i in _phrases(question):
        positions = set(range(start_i, end_i))
        if positions & taken:
            continue
        if all(word.casefold() in STOPWORDS for word in phrase.split()):
            continue
        for family, attribute in SCAN_FAMILIES:
            hit = vocab.match(family, phrase)
            if hit is None:
                continue
            sink: list[str] = getattr(found, attribute)
            if hit not in sink:
                sink.append(hit)
            taken |= positions
            break

    # Codes are matched exactly: D032, BV-0104 and PR-2026-005 are identifiers, not words.
    for code in _CODE.findall(question):
        upper = code.upper()
        if upper in vocab.of("distributor_ids") and upper not in found.distributors:
            found.distributors.append(upper)
        elif upper in vocab.of("sku_codes") and upper not in found.skus:
            found.skus.append(upper)
        elif upper in vocab.of("promo_ids") and upper not in found.promo_ids:
            found.promo_ids.append(upper)
        elif upper in vocab.of("territory_codes") and upper not in found.territories:
            found.territories.append(upper)

    return found, unknown_proper_nouns(question, vocab)


def unknown_proper_nouns(question: str, vocab: Vocabulary) -> list[str]:
    """Capitalised words, not sentence-initial, that no vocabulary and no month knows."""
    unknown: list[str] = []
    for word in _PROPER_NOUN.findall(question):
        lowered = word.casefold()
        if lowered in STOPWORDS or lowered in _MONTH_NUMBERS or lowered in NON_ENTITY_PROPER:
            continue
        if vocab.knows(word):
            continue
        # A word inside a name the scanner already matched is not unknown.
        if any(
            normalise(word) in normalise(value)
            for family in vocab.index
            for value in vocab.of(family)
        ):
            continue
        if word not in unknown:
            unknown.append(word)
    return unknown


# ---------------------------------------------------------------------------
# The stage itself
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Refusal:
    """A decision not to answer, made before any provider call."""

    reason: str
    message: str
    evidence: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class Resolution:
    """What the question refers to, or why it cannot be processed."""

    entities: Entities
    period: Period
    refusal: Refusal | None = None


def _find_unsupported_metric(question: str) -> str | None:
    """Return the plain-English name of an unsupported metric the question asks for."""
    text = f" {re.sub(r'[^a-z0-9 -]', ' ', question.casefold())} "
    text = re.sub(r"\s+", " ", text)
    for phrase, label in UNSUPPORTED_METRICS.items():
        if f" {phrase} " in text:
            return label
    return None


def resolve(question: str, vocab: Vocabulary) -> Resolution:
    """Resolve entities and period, or refuse — all before a token is spent.

    Refusal precedence is fixed so that a question tripping two classes always reports the
    same one: unsupported metric, then out of period, then unknown entity. The order runs
    from the most fundamental objection to the least — a question about margin cannot be
    answered for any entity in any period, so naming the missing metric is more useful
    than naming a brand that is also absent.
    """
    metric = _find_unsupported_metric(question)
    if metric is not None:
        return Resolution(
            entities=Entities(),
            period=FULL_YEAR,
            refusal=Refusal(
                reason=UNSUPPORTED_METRIC,
                message=(
                    f"The data pack holds no {metric}. It covers {SUPPORTED_METRICS} for "
                    f"{FY26_LABEL}, and a figure that is not in it will not be estimated."
                ),
            ),
        )

    parsed = parse_period(question)
    if parsed.period is None:
        return Resolution(
            entities=Entities(),
            period=FULL_YEAR,
            refusal=Refusal(
                reason=OUT_OF_PERIOD,
                message=(
                    f"{parsed.out_of_period} is outside the data. The pack covers "
                    f"{FY26_LABEL} only."
                ),
            ),
        )

    entities, unknown = scan_entities(question, vocab)
    if unknown:
        # Refused even where the question also named something real. "How did Blipco do in
        # the North?" resolves the North, but answering it would silently report the whole
        # region as though it were the brand that was asked about.
        plural = "are" if len(unknown) > 1 else "is"
        return Resolution(
            entities=entities,
            period=parsed.period,
            refusal=Refusal(
                reason=UNKNOWN_ENTITY,
                message=(
                    f"{_join(unknown)} {plural} not in the data. "
                    f"The pack holds {len(vocab.of('brands'))} brands across "
                    f"{len(vocab.of('regions'))} regions and "
                    f"{len(vocab.of('territories'))} territories."
                ),
            ),
        )

    return Resolution(entities=entities, period=parsed.period)


def _join(values: Sequence[str] | Iterable[str]) -> str:
    """Render a short list as English: ``a``, ``a and b``, ``a, b and c``."""
    items = list(values)
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"
