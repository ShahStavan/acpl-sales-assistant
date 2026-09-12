"""Read the raw data from the 7 CSV exports, ``action_playbook.xlsx`` and 6 ``.docx`` documents.

Dates are parsed with explicit formats per source (ISO, ``YYYY-MM``, ``DD/MM/YYYY``).
Documents are held whole and tagged with brands, regions, distributors and months by
whole-word vocabulary match. DESIGN.md §4.4 item 4, §4.5.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from pathlib import Path
import re

from docx import Document
import pandas as pd

from acpl_assistant.config import get_settings


def _month_vocab() -> set[str]:
    return {calendar.month_name[i] for i in range(1, 13)} | {
        calendar.month_abbr[i] for i in range(1, 13)
    }


# ---------------------------------------------------------------------------
# Year resolution helpers
# ---------------------------------------------------------------------------

# Map every month name/abbr to its 1-based month number.
_MONTH_NUMBERS: dict[str, int] = {}
for _i in range(1, 13):
    _MONTH_NUMBERS[calendar.month_name[_i].casefold()] = _i
    _MONTH_NUMBERS[calendar.month_abbr[_i].casefold()] = _i


def _parse_year_from_text(text: str, month_key: str) -> int | None:
    """Look for a 4-digit year next to *month_key* (case-insensitive)."""
    pat = re.compile(rf"{re.escape(month_key)}\s*(?:20|19)\d\d", re.IGNORECASE)
    m = pat.search(text)
    if m:
        digits = re.search(r"(20|19)\d\d", m.group())
        if digits:
            return int(digits.group())
    return None


def _parse_year_from_filename(filename: str) -> int | None:
    m = re.search(r"(20|19)\d\d", filename)
    return int(m.group()) if m else None


def _resolve_month_mentions(text: str, source_file: str) -> list[str]:
    """Resolve months in *text* to ``YYYY-MM`` keys.

    Year is drawn from an explicit 4-digit year next to the month,
    or from *source_file*, or the mention is left out.
    Abbreviated and full month names collapse into one key per month.
    """
    seen_months: dict[int, int] = {}  # month_number -> year
    month_pat = re.compile(
        r"\b("
        + "|".join(
            re.escape(calendar.month_name[i]) + "|" + re.escape(calendar.month_abbr[i])
            for i in range(1, 13)
        )
        + r")\b",
        re.IGNORECASE,
    )
    for match in month_pat.finditer(text):
        raw = match.group(1)
        month_num = _MONTH_NUMBERS.get(raw.lower())
        if month_num is None:
            continue
        if month_num in seen_months:
            continue
        year = _parse_year_from_text(text, raw)
        if year is None:
            year = _parse_year_from_filename(source_file)
        if year is None:
            continue
        seen_months[month_num] = year

    return sorted(f"{y:04d}-{m:02d}" for m, y in seen_months.items())


# ---------------------------------------------------------------------------
# _tag_doc
# ---------------------------------------------------------------------------


def _tag_doc(
    text: str,
    brands: set[str],
    regions: set[str],
    distributors: set[str],
) -> dict[str, list[str]]:
    months = _month_vocab()
    found_months = sorted(m for m in months if re.search(r"\b" + re.escape(m) + r"\b", text))
    return {
        "brands": sorted(b for b in brands if re.search(r"\b" + re.escape(b) + r"\b", text)),
        "regions": sorted(r for r in regions if re.search(r"\b" + re.escape(r) + r"\b", text)),
        "distributors": sorted(
            d for d in distributors if re.search(r"\b" + re.escape(d) + r"\b", text)
        ),
        "months": found_months,
    }


# ---------------------------------------------------------------------------
# Individual readers
# ---------------------------------------------------------------------------


def read_fact_primary_sales(path: Path) -> pd.DataFrame:
    """Read primary-sales ledger with ISO week_start."""
    return pd.read_csv(
        path / "fact_primary_sales.csv",
        dtype={
            "sku_code": "str",
            "territory_code": "str",
            "units": "int64",
            "value_inr": "float64",
        },
        parse_dates=["week_start"],
        date_format="%Y-%m-%d",
    )


def read_fact_targets(path: Path) -> pd.DataFrame:
    """Read target plan with ``YYYY-MM`` month parsed to datetime."""
    df = pd.read_csv(
        path / "fact_targets.csv",
        dtype={
            "brand_name": "str",
            "region_name": "str",
            "target_value_inr": "int64",
        },
    )
    df["month"] = pd.to_datetime(df["month"], format="%Y-%m")
    return df


def read_stockouts(path: Path) -> pd.DataFrame:
    """Read stock-out log with ISO week_start; key is ``item_code``."""
    return pd.read_csv(
        path / "stockouts.csv",
        dtype={
            "distributor_id": "str",
            "item_code": "str",
            "region": "str",
            "days_out_of_stock": "int64",
        },
        parse_dates=["week_start"],
        date_format="%Y-%m-%d",
    )


def read_promotions(path: Path) -> pd.DataFrame:
    """Read trade-promotion calendar; ``start_date`` / ``end_date`` in ``DD/MM/YYYY``."""
    return pd.read_csv(
        path / "promotions.csv",
        dtype={
            "promo_id": "str",
            "sku": "str",
            "region": "str",
            "discount_pct": "int64",
            "mechanic": "str",
        },
        parse_dates=["start_date", "end_date"],
        date_format="%d/%m/%Y",
    )


def read_dim_sku(path: Path) -> pd.DataFrame:
    """Read product master (120 SKUs)."""
    return pd.read_csv(
        path / "dim_sku.csv",
        dtype={
            "sku_code": "str",
            "sku_name": "str",
            "brand": "str",
            "category": "str",
            "pack_size": "str",
            "mrp_inr": "int64",
        },
    )


def read_dim_geo(path: Path) -> pd.DataFrame:
    """Read geography master (12 territories)."""
    return pd.read_csv(
        path / "dim_geo.csv",
        dtype={
            "territory_code": "str",
            "territory_name": "str",
            "region": "str",
            "state": "str",
        },
    )


def read_dim_distributor(path: Path) -> pd.DataFrame:
    """Read distributor master (40 distributors)."""
    return pd.read_csv(
        path / "dim_distributor.csv",
        dtype={
            "distributor_id": "str",
            "distributor_name": "str",
            "territory_code": "str",
            "city": "str",
        },
    )


def read_documents(
    path: Path, dim_sku: pd.DataFrame, dim_geo: pd.DataFrame, dim_dist: pd.DataFrame
) -> pd.DataFrame:
    """Load ``.docx`` files and tag each with matched brands, regions, distributors and months."""
    brands = set(dim_sku["brand"].unique())
    regions = set(dim_geo["region"].unique())
    distributors = set(dim_dist["distributor_id"].unique())

    docs_dir = path / "documents"
    rows = []
    for fpath in sorted(docs_dir.iterdir()):
        if fpath.suffix.lower() != ".docx":
            continue
        doc = Document(str(fpath))
        text = "\n".join(p.text for p in doc.paragraphs)
        tags = _tag_doc(text, brands, regions, distributors)
        resolved = _resolve_month_mentions(text, fpath.name)
        rows.append(
            {
                "source_file": fpath.name,
                "text": text,
                "brands": ", ".join(tags["brands"]),
                "regions": ", ".join(tags["regions"]),
                "distributors": ", ".join(tags["distributors"]),
                "months": ", ".join(tags["months"]),
                "months_resolved": ", ".join(resolved),
            }
        )
    return pd.DataFrame(
        rows,
        columns=[
            "source_file",
            "text",
            "brands",
            "regions",
            "distributors",
            "months",
            "months_resolved",
        ],
    )


def read_playbook(path: Path) -> pd.DataFrame:
    """Read action playbook from the ``playbook`` sheet, parsing ``needs_approval`` to bool."""
    df = pd.read_excel(
        path / "action_playbook.xlsx",
        sheet_name="playbook",
        dtype={"rule_id": "str", "condition": "str", "recommendation": "str", "action": "str"},
    )
    df["needs_approval"] = df["needs_approval"].map({"Yes": True, "No": False})
    return df


# ---------------------------------------------------------------------------
# RawPack
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RawPack:
    """Frozen container holding all raw DataFrames loaded from the data pack."""

    fact_primary_sales: pd.DataFrame
    fact_targets: pd.DataFrame
    stockouts: pd.DataFrame
    promotions: pd.DataFrame
    dim_sku: pd.DataFrame
    dim_geo: pd.DataFrame
    dim_distributor: pd.DataFrame
    documents: pd.DataFrame
    playbook: pd.DataFrame


def load_raw_pack(data_dir: Path | None = None) -> RawPack:
    """Load every source in the data pack and return a :class:`RawPack`."""
    if data_dir is None:
        data_dir = get_settings().acpl_data_dir_resolved

    sales = read_fact_primary_sales(data_dir)
    targets = read_fact_targets(data_dir)
    stockouts_df = read_stockouts(data_dir)
    promos = read_promotions(data_dir)
    sku = read_dim_sku(data_dir)
    geo = read_dim_geo(data_dir)
    dist = read_dim_distributor(data_dir)

    docs = read_documents(data_dir, sku, geo, dist)
    play = read_playbook(data_dir)

    return RawPack(
        fact_primary_sales=sales,
        fact_targets=targets,
        stockouts=stockouts_df,
        promotions=promos,
        dim_sku=sku,
        dim_geo=geo,
        dim_distributor=dist,
        documents=docs,
        playbook=play,
    )
