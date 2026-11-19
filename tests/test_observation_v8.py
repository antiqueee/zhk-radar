import json
from pathlib import Path

import pytest

from radar.reports.observation_v8 import observation_available, observation_data


@pytest.fixture
def watch():
    path = Path(__file__).parent / "fixtures/report-research-v5.json"
    materials = json.loads(path.read_text())
    sources = [
        {"kind": "page", **source} for source in materials["sources"]
    ]
    return {
        "name": "Объект из поиска",
        "region": "Москва",
        "note": "Частная заметка",
        "report_research": {"completed": True, "errors": []},
        "report_pipeline": {
            "version": "report-kit-v8", "passed": False, "status": "watch", "issues": [],
            "sources": sources, "facts": {"facts": materials["facts"]},
        },
        "report_qualification": {
            "verdict": "watch",
            "checked": "2026-11-19",
            "coverage": {"dated_evidence_12m": 2, "active_contours": ["reviews"]},
            "research_evidence": materials["research"],
            "manual_checks": ["Недоказанное предположение"],
        },
    }


def test_observation_copies_only_linked_public_evidence_and_supplied_official_proof(watch):
    assert observation_available(watch)
    official = {
        "title": "Сайт застройщика", "quote": "Выдача ключей, корпус 5 — I квартал 2027 года.",
        "url": "https://developer.test/project",
    }
    data = observation_data(watch, evidence=[
        official, official, {**official, "url": "http://127.0.1/report"},
        {**official, "url": "https://t.me/private"},
        {**official, "url": "https://example.test/report?token=private"},
    ])
    assert data["meta"] == {
        "schema_version": "8", "mode": "observation", "date": "2026-11-19", "layout": "compact",
    }
    assert data["qualification"]["dated_evidence"] == 2
    assert data["qualification"]["active_contours"] == ["Отзывы"]
    assert "Стадия конфликта не подтверждена" in data["qualification"]["detail"]
    assert set(row["source_n"] for row in data["signals"]) == set(
        row["n"] for row in data["sources"]
    )
    assert data["official"] == [{"text": official["quote"], "title": official["title"],
                                 "url": official["url"]}]
    assert data["signals"] and len(data["signals"]) <= 4
    assert all("t.me" not in src["url"] for src in data["sources"])
    saved_text = json.dumps(data, ensure_ascii=False)
    assert "Частная заметка" not in saved_text and "Недоказанное предположение" not in saved_text
    for signal in data["signals"]:
        source = next(row for row in watch["report_pipeline"]["sources"]
                      if row["n"] == signal["source_n"])
        assert signal["text"].removesuffix("…") in " ".join(source["text"].split())


@pytest.mark.parametrize("section,field,value", [
    ("report_pipeline", "version", "report-kit-v5"),
    ("report_pipeline", "issue_kind", "model_output"),
    ("report_pipeline", "issues", ["Генерация прервалась"]),
    ("report_pipeline", "sources", []),
    ("report_qualification", "verdict", "hold"),
    ("report_research", "completed", False),
    ("report_research", "errors", [{"stage": "search"}]),
])
def test_observation_does_not_publish_incomplete_or_ineligible_snapshots(watch, section, field,
                                                                  value):
    watch[section][field] = value
    assert not observation_available(watch)
    with pytest.raises(ValueError):
        observation_data(watch)


def test_observation_needs_an_exact_quote_from_the_same_saved_source(watch):
    assert not observation_available(None)
    for fact in watch["report_pipeline"]["facts"]["facts"]:
        fact["verbatim"] = "Сгенерированная цитата, которой нет ни в одном источнике."
    assert not observation_available(watch)


def test_observation_can_use_a_precise_saved_quote_from_the_same_publication(watch):
    initial = observation_data(watch)
    source = initial["sources"][0]
    saved_source = next(row for row in watch["report_pipeline"]["sources"]
                        if row["n"] == source["n"])
    quote = "Передачу квартир ожидают весной."
    saved_source["text"] += "\n" + quote
    watch["evidence"] = [
        {"url": source["url"], "quote": "Частный неподтверждённый вывод."},
        {"url": source["url"] + "/", "quote": quote},
    ]
    result = observation_data(watch)
    assert result["signals"][0]["text"] == quote
