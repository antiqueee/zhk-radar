import json
import shutil

import pytest

from radar.web import stoplist
from radar.web.brand_analytics_queries import build_queries
from radar.web.report_search import initialize as initialize_research
from radar.web.store import Store, now


@pytest.fixture
def catalog(tmp_path):
    shutil.copytree("config", tmp_path / "config")
    return Store(tmp_path)


def add_card(catalog, ident, name, *, dataset="web", **extra):
    card = {"name": name, **extra}
    with catalog.connect() as db:
        db.execute(
            "INSERT INTO records(id,dataset,payload,updated_at) VALUES(?,?,?,?)",
            (ident, dataset, json.dumps(card, ensure_ascii=False), now()),
        )


def test_builds_one_shared_query_with_safe_known_aliases_and_no_duplicate_phrases(catalog):
    add_card(catalog, "one", "ЖК «Нагатино Ай-Лэнд»", developer="Эталон")
    add_card(catalog, "two", "НАГАТИНО АЙ-ЛЭНД", dataset="imported", developer="Эталон")
    initialize_research(catalog)
    payload = {"passport": {"aliases": ["Nagatino I-Land", 'Sky "Tower", | / \n Old']}}
    with catalog.connect() as db:
        db.execute(
            "INSERT INTO report_research VALUES(?,?,?)",
            ("one", json.dumps(payload), now()),
        )
    result = build_queries(catalog)
    assert result == build_queries(catalog)
    assert result["total_cards"] == result["covered_cards"] == 2
    assert result["uncovered"] == []
    [topic] = result["queries"]
    assert topic["characters"] == len(topic["query"])
    assert topic["card_ids"] == ["one", "two"]
    assert {term.casefold() for term in topic["query"].split(", ")} == {
        '"nagatino i land"~0',
        '"нагатино ай лэнд"~0',
        '"sky tower old"~0',
    }


def test_shared_name_needs_context_and_different_phases_can_share_one_clause(catalog):
    add_card(catalog, "a1", "ЖК Баланс", developer="Эталон", region="Москва")
    add_card(catalog, "a2", "ЖК Баланс, корпус 2", developer="Эталон", region="Москва")
    add_card(catalog, "b", "Баланс", developer="ФСК", region="Тула")
    add_card(catalog, "unknown", "Баланс", region="Россия")
    result = build_queries(catalog)
    [topic] = result["queries"]
    terms = {term.casefold() for term in topic["query"].split(", ")}
    assert '"баланс"~0 "эталон"~0' in terms
    assert '"баланс"~0 "фск"~0' in terms
    assert '"баланс"~0' not in terms
    assert topic["card_ids"] == ["a1", "a2", "b"]
    assert result["uncovered"] == [
        {"id": "unknown", "name": "Баланс", "reason": "ambiguous_without_context"}
    ]


def test_reports_scope_and_capacity_without_creating_tables(catalog):
    add_card(catalog, "one", "Медовый бор")
    add_card(catalog, "two", "Садовые башни")
    add_card(catalog, "generic", "Портфель по области")
    add_card(catalog, "merge", "Старая карточка", merged_into="one")
    add_card(catalog, "developer", "ФСК", dataset="imported", granularity="developer")
    result = build_queries(catalog, max_chars=19, max_topics=2)
    assert result["total_cards"] == 3
    assert result["covered_cards"] == 2
    assert all(topic["characters"] <= 19 for topic in result["queries"])
    assert len(result["queries"]) == 2
    assert result["uncovered"] == [
        {"id": "generic", "name": "Портфель по области", "reason": "no_project_name"}
    ]
    one_topic = build_queries(catalog, max_chars=19, max_topics=1)
    assert one_topic["covered_cards"] == one_topic["omitted_expressions"] == 1
    assert any(row["reason"] == "query_capacity" for row in one_topic["uncovered"])
    with catalog.connect() as db:
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE name IN ('stoplist','report_research')"
        ).fetchall() == []


def test_respects_stoplist_for_record_and_developer(catalog):
    add_card(catalog, "included", "Новые Дюны", developer="Самолёт")
    add_card(catalog, "blocked", "Облачный Берег", developer="Самолёт")
    add_card(catalog, "partner", "Медовая долина", developer="Девелопер А")
    stoplist.add(catalog, stoplist.StopEntry(kind="record", value="blocked"))
    stoplist.add(catalog, stoplist.StopEntry(kind="developer", value="Девелопер А"))
    result = build_queries(catalog)
    assert result["total_cards"] == result["covered_cards"] == 1
    assert result["queries"][0]["card_ids"] == ["included"]


@pytest.mark.parametrize("options", [{"max_chars": 0}, {"max_topics": 3}])
def test_rejects_invalid_limits(catalog, options):
    with pytest.raises(ValueError, match="предел длины"):
        build_queries(catalog, **options)
