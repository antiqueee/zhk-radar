import json
from pathlib import Path

import pytest

from radar.reports.fact_spans import source_span

ROOT = Path(__file__).resolve().parents[1]


def test_first_cached_live_response_restores_six_exact_source_spans():
    rows = json.loads((ROOT / "tests/fixtures/facts-source-span-regression.json").read_text())
    assert [r["cached_fact_index"] for r in rows] == [0, 6, 10, 18, 22, 23]
    for row in rows:
        result = source_span(row["model_verbatim"], row["source_text"])
        assert result == row["source_text"]
        assert result in row["source_text"]


def test_unicode_and_whitespace_mapping_preserves_original_source():
    source = "Этот раи\u0306он\u00a0\u00a0обсуждает\n\tсроки выдачи ключей."
    quote = "Этот район обсуждает сроки выдачи ключей."
    assert source_span(quote, source) == source


@pytest.mark.parametrize(
    "quote,source",
    [
        ("Дольщики ждут ключи", "дольщики ждут ключи"),
        ("Объект — жилой комплекс", "Объект - жилой комплекс"),
        ("Дольщики требуют назвать дату", "Дольщики ждут, когда назовут дату"),
        (
            "План передачи помещений на декабрь 2026.",
            "План передачи помещений на декабрь 2026, срок уточнён",
        ),
        (
            "План передачи помещений назван на 2026 г.",
            "План передачи помещений назван на 2026 г, срок уточнён",
        ),
        ("Передача ключей. Все даты", "Передача ключей, все даты"),
    ],
)
def test_words_case_internal_punctuation_and_numeric_endings_are_not_repaired(quote, source):
    assert source_span(quote, source) is None


def test_ambiguous_whitespace_mapping_is_not_accepted():
    source = "Дольщики\nждут ключи от квартир; Дольщики\tждут ключи от квартир"
    assert source_span("Дольщики ждут ключи от квартир", source) is None


def test_only_one_model_added_full_stop_is_removed_for_a_unique_long_body():
    body = "Достройкой жилого комплекса занимается компания за счёт застройщика"
    source = body + ", сроки уточнят позднее."
    assert source_span(body + ".", source) == body
    assert source_span(body + "!", source) is None
    assert source_span("Работы идут.", "Работы идут, срок назовут") is None


def test_live_dogma_table_restores_the_two_deleted_row_breaks_exactly():
    row = json.loads((ROOT / "tests/fixtures/dogma-format-regression.json").read_text())["table"]
    restored = source_span(row["model_verbatim"], row["source_text"])
    assert restored == row["expected_span"]
    assert restored.count("\n") == 2
    assert restored in row["source_text"]


def test_row_joining_never_changes_non_whitespace_or_chooses_an_ambiguous_span():
    row = json.loads((ROOT / "tests/fixtures/dogma-format-regression.json").read_text())["table"]
    quote, source = row["model_verbatim"], row["source_text"]
    assert source_span(quote.replace("Литер 23", "Литер 24"), source) is None
    assert source_span(quote.replace("+1 кв.", "+2 кв."), source) is None
    assert source_span(quote.lower(), source) is None
    assert source_span(quote, source + "\n" + row["expected_span"]) is None
    assert source_span("Срокипередачи", "Сроки\nпередачи") is None  # too short to restore
