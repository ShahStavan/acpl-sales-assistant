"""Entity and period resolution against the real vocabularies.

Everything here happens before a token is spent, which is what makes three of the five
refusal classes free and reproducible. The vocabularies come from the warehouse rather than
from a fixture, because the thing under test is whether "Aloe Vera Glow" and "Delhi NCR"
survive contact with the names the pack actually contains.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest

from acpl_assistant.ask.resolve import (
    ALL_MONTHS,
    CURRENT_MONTH,
    DATA_LATEST_WEEK,
    FISCAL_QUARTERS,
    FULL_YEAR,
    OUT_OF_PERIOD,
    UNKNOWN_ENTITY,
    UNSUPPORTED_METRIC,
    Vocabulary,
    build_vocabulary,
    normalise,
    parse_period,
    parse_periods,
    resolve,
    scan_entities,
)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def vocab(prepared: tuple[Path, dict]) -> Iterator[Vocabulary]:
    """Every controlled vocabulary, read from the prepared warehouse."""
    db_path, _ = prepared
    con = duckdb.connect(str(db_path), read_only=True)
    yield build_vocabulary(con)
    con.close()


# ---------------------------------------------------------------------------
# The temporal frame
# ---------------------------------------------------------------------------


class TestPeriods:
    def test_relative_time_anchors_to_the_data_not_the_clock(self) -> None:
        """Clock-anchored resolution would expire the brief's own example question."""
        assert DATA_LATEST_WEEK.isoformat() == "2026-06-23"
        assert parse_period("this quarter").period.months == FISCAL_QUARTERS[4]
        assert parse_period("last quarter").period.months == FISCAL_QUARTERS[3]
        assert parse_period("this month").period.months == (CURRENT_MONTH,)
        assert parse_period("last month").period.months == ("2026-05",)

    @pytest.mark.parametrize("number", [1, 2, 3, 4])
    def test_each_fiscal_quarter_reads_as_its_own_months(self, number: int) -> None:
        parsed = parse_period(f"How did we do in Q{number}?")
        assert parsed.period.months == FISCAL_QUARTERS[number]
        assert parsed.period.label == f"FY26 Q{number}"

    def test_the_fiscal_year_starts_in_july(self) -> None:
        assert FISCAL_QUARTERS[1][0] == "2025-07"
        assert FISCAL_QUARTERS[4][-1] == "2026-06"

    def test_halves_split_the_year(self) -> None:
        assert parse_period("H1 performance").period.months == ALL_MONTHS[:6]
        assert parse_period("H2 FY26").period.months == ALL_MONTHS[6:]

    @pytest.mark.parametrize(
        ("question", "month"),
        [
            ("What happened in February?", "2026-02"),
            ("What happened in August?", "2025-08"),
            ("sales in Feb 2026", "2026-02"),
            ("sales in 2025-11", "2025-11"),
        ],
    )
    def test_a_named_month_needs_no_year_inside_the_fiscal_year(
        self, question: str, month: str
    ) -> None:
        """July to December are 2025 and January to June 2026, so a bare month is unambiguous."""
        assert parse_period(question).period.months == (month,)

    def test_a_question_with_no_period_covers_the_whole_year(self) -> None:
        assert parse_period("Which brand sold most?").period == FULL_YEAR

    @pytest.mark.parametrize("phrase", ["FY26", "this year", "the full year", "this FY"])
    def test_the_year_can_be_named_several_ways(self, phrase: str) -> None:
        assert parse_period(f"sales for {phrase}").period == FULL_YEAR

    @pytest.mark.parametrize(
        ("question", "named"),
        [
            ("sales in 2019-03", "2019-03"),
            ("How did we do in FY24?", "FY24"),
            ("sales in July 2026", "July 2026"),
            ("What were sales in 2019?", "2019"),
            ("Q1 of 2019", "2019"),
            ("Q1 FY24", "FY24"),
        ],
    )
    def test_anything_outside_fy26_is_named_back(self, question: str, named: str) -> None:
        parsed = parse_period(question)
        assert parsed.period is None
        assert parsed.out_of_period == named


class TestTwoPeriods:
    def test_a_comparison_yields_both_sides_in_order(self) -> None:
        found = parse_periods("How did Beverages move from Q3 to Q4?")
        assert [p.label for p in found] == ["FY26 Q3", "FY26 Q4"]

    def test_one_period_named_twice_is_still_one_period(self) -> None:
        assert len(parse_periods("Q4 versus Q4")) == 1

    def test_named_months_compare_too(self) -> None:
        found = parse_periods("Compare February with March")
        assert [p.label for p in found] == ["2026-02", "2026-03"]

    def test_a_single_period_question_yields_one(self) -> None:
        assert len(parse_periods("How did we do in Q4?")) == 1

    def test_an_out_of_range_reference_is_dropped_rather_than_refused(self) -> None:
        """``parse_period`` has already refused the question if the *first* period is outside."""
        assert [p.label for p in parse_periods("Q4 versus 2019-03")] == ["FY26 Q4"]


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


class TestEntities:
    def test_a_brand_is_found(self, vocab: Vocabulary) -> None:
        found, unknown = scan_entities("How did Aqualite do?", vocab)
        assert found.brands == ["Aqualite"]
        assert not unknown

    @pytest.mark.parametrize(
        "name", ["Aloe Vera Glow", "Namkeen Nation", "Marigold Marie", "Zing Cola"]
    )
    def test_a_multi_word_name_matches_as_a_whole(self, name: str, vocab: Vocabulary) -> None:
        """A scanner that offered only single words would find "Glow" and nothing else."""
        found, _ = scan_entities(f"How did {name} do in Q4?", vocab)
        assert found.brands == [name]

    def test_a_multi_word_territory_matches(self, vocab: Vocabulary) -> None:
        found, unknown = scan_entities("sales in Delhi NCR", vocab)
        assert found.territories == ["Delhi NCR"]
        assert not unknown

    def test_a_category_matches(self, vocab: Vocabulary) -> None:
        found, _ = scan_entities("Which Home Care SKUs sold most?", vocab)
        assert found.categories == ["Home Care"]

    def test_several_families_in_one_question(self, vocab: Vocabulary) -> None:
        found, _ = scan_entities("Aqualite in West in Q4", vocab)
        assert found.brands == ["Aqualite"]
        assert found.regions == ["West"]

    @pytest.mark.parametrize(
        ("code", "family"),
        [
            ("D032", "distributors"),
            ("BV-0104", "skus"),
            ("PR-2026-005", "promo_ids"),
        ],
    )
    def test_identifiers_are_matched_exactly(
        self, code: str, family: str, vocab: Vocabulary
    ) -> None:
        found, _ = scan_entities(f"Tell me about {code}.", vocab)
        assert getattr(found, family) == [code]

    def test_a_promotion_id_is_not_read_as_a_shorter_code(self, vocab: Vocabulary) -> None:
        """ "PR-2026-005" consumed as "PR-2026" would match no promotion at all."""
        found, _ = scan_entities("How did PR-2026-005 do?", vocab)
        assert found.promo_ids == ["PR-2026-005"]
        assert not found.skus

    @pytest.mark.parametrize("typo", ["Aqualitte", "Aqalite", "Aqualit"])
    def test_a_near_typo_still_resolves(self, typo: str, vocab: Vocabulary) -> None:
        found, unknown = scan_entities(f"How did {typo} do?", vocab)
        assert found.brands == ["Aqualite"]
        assert not unknown

    @pytest.mark.parametrize("name", ["Aqualight", "Aqualtie"])
    def test_a_name_beyond_the_threshold_is_unknown_rather_than_guessed(
        self, name: str, vocab: Vocabulary
    ) -> None:
        """Below FUZZY_ACCEPT the honest answer is a refusal, not the nearest brand."""
        found, unknown = scan_entities(f"How did {name} do?", vocab)
        assert not found.brands
        assert unknown == [name]

    def test_a_domain_phrase_is_not_read_as_a_brand(self, vocab: Vocabulary) -> None:
        """ "losing most against target" must not fuzzy-match "Home Care"."""
        found, unknown = scan_entities("Where are we losing most against target?", vocab)
        assert not found.any_named
        assert not unknown

    def test_a_mechanic_is_an_entity_family_of_its_own(self, vocab: Vocabulary) -> None:
        found, _ = scan_entities("Which Buy 2 Get 1 promotions worked?", vocab)
        assert found.mechanics == ["Buy 2 Get 1"]

    def test_as_dict_drops_the_empty_families(self, vocab: Vocabulary) -> None:
        found, _ = scan_entities("Aqualite in West", vocab)
        assert set(found.as_dict()) == {"brands", "regions"}
        assert found.any_named

    def test_a_question_naming_nothing_names_nothing(self, vocab: Vocabulary) -> None:
        found, _ = scan_entities("How are we doing?", vocab)
        assert not found.any_named


class TestUnknownProperNouns:
    @pytest.mark.parametrize("name", ["Zorblax", "Atlantis", "Blipco"])
    def test_a_proper_noun_the_pack_lacks_is_reported(self, name: str, vocab: Vocabulary) -> None:
        _, unknown = scan_entities(f"How did {name} do in Q4?", vocab)
        assert unknown == [name]

    def test_a_sentence_initial_word_is_not_a_proper_noun(self, vocab: Vocabulary) -> None:
        """English capitalises the first word regardless of what it is."""
        _, unknown = scan_entities("Which brands sold the most?", vocab)
        assert not unknown

    @pytest.mark.parametrize("word", ["INR", "FY26", "SKU", "India", "Monday"])
    def test_ordinary_capitalised_words_are_not_entities(
        self, word: str, vocab: Vocabulary
    ) -> None:
        _, unknown = scan_entities(f"Show me {word} figures for Q4.", vocab)
        assert not unknown

    def test_a_month_name_is_not_an_unknown_entity(self, vocab: Vocabulary) -> None:
        _, unknown = scan_entities("What happened in February?", vocab)
        assert not unknown

    def test_a_word_inside_a_matched_name_is_not_unknown(self, vocab: Vocabulary) -> None:
        _, unknown = scan_entities("How did Aloe Vera Glow do?", vocab)
        assert not unknown


class TestVocabulary:
    def test_normalise_ignores_case_and_punctuation(self) -> None:
        assert normalise("Delhi  NCR") == normalise("delhi ncr")

    def test_the_pack_holds_what_the_refusal_claims(self, vocab: Vocabulary) -> None:
        assert len(vocab.of("brands")) == 15
        assert len(vocab.of("regions")) == 4
        assert len(vocab.of("territories")) == 12

    def test_an_unknown_family_is_empty_rather_than_an_error(self, vocab: Vocabulary) -> None:
        assert vocab.of("nonesuch") == ()

    def test_knows_spans_every_family(self, vocab: Vocabulary) -> None:
        assert vocab.knows("Aqualite")
        assert vocab.knows("West")
        assert not vocab.knows("Zorblax")


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------


class TestResolve:
    def test_a_resolvable_question_is_not_refused(self, vocab: Vocabulary) -> None:
        resolution = resolve("How did Aqualite do in West in Q4?", vocab)
        assert resolution.refusal is None
        assert resolution.entities.brands == ["Aqualite"]
        assert resolution.period.label == "FY26 Q4"

    def test_an_unsupported_metric_outranks_everything(self, vocab: Vocabulary) -> None:
        """Margin cannot be answered for any entity in any period, so say that first."""
        resolution = resolve("What was Zorblax's gross margin in 2019?", vocab)
        assert resolution.refusal.reason == UNSUPPORTED_METRIC
        assert "margin" in resolution.refusal.message

    def test_an_out_of_period_question_outranks_an_unknown_name(self, vocab: Vocabulary) -> None:
        resolution = resolve("How did Zorblax do in 2019?", vocab)
        assert resolution.refusal.reason == OUT_OF_PERIOD

    def test_an_unknown_name_is_refused_even_alongside_a_real_one(self, vocab: Vocabulary) -> None:
        """Answering would silently report the region as though it were the brand asked for."""
        resolution = resolve("How did Blipco do in the North?", vocab)
        assert resolution.refusal.reason == UNKNOWN_ENTITY
        assert "Blipco" in resolution.refusal.message

    def test_the_refusal_says_what_the_pack_does_hold(self, vocab: Vocabulary) -> None:
        message = resolve("How did Zorblax do?", vocab).refusal.message
        assert "15 brands" in message
        assert "4 regions" in message

    def test_two_unknown_names_read_as_a_list(self, vocab: Vocabulary) -> None:
        message = resolve("Compare Zorblax and Blipco in Q4.", vocab).refusal.message
        assert "Zorblax and Blipco are not in the data" in message

    @pytest.mark.parametrize(
        ("question", "phrase"),
        [
            ("What was our gross margin?", "margin"),
            ("What is the market share of Aqualite?", "market share"),
            ("What was the ROI on our promotions?", "promotion ROI"),
            ("What are secondary sales for West?", "secondary sales"),
            ("Forecast next quarter for Aqualite.", "a forecast"),
            ("How profitable was West in Q4?", "profit"),
        ],
    )
    def test_each_absent_metric_is_named_in_its_refusal(
        self, question: str, phrase: str, vocab: Vocabulary
    ) -> None:
        resolution = resolve(question, vocab)
        assert resolution.refusal.reason == UNSUPPORTED_METRIC
        assert phrase in resolution.refusal.message

    def test_a_refused_question_still_reports_a_period(self, vocab: Vocabulary) -> None:
        """The caller gets a usable ``Resolution`` whatever happened; only ``refusal`` varies."""
        assert resolve("What was our margin?", vocab).period == FULL_YEAR
