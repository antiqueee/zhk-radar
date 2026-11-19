"""Offline projection checks: safe, stable v5 tabs alongside the existing register."""

import json
import shutil
from pathlib import Path

import pytest

from radar.web import sheets
from radar.web.report_evidence import qualify
from radar.web.store import Store, now


@pytest.fixture
def store(tmp_path):
    shutil.copytree(Path("config"), tmp_path / "config")
    value = Store(tmp_path)
    with value.connect() as db:
        db.execute("UPDATE records SET dataset='web' WHERE id='demo-1'")
    return value


def research(store, payload):
    with store.connect() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS report_research("
            "record_id TEXT PRIMARY KEY,payload TEXT,updated_at TEXT)"
        )
        db.execute(
            "INSERT OR REPLACE INTO report_research VALUES(?,?,?)",
            ("demo-1", json.dumps(payload), now()),
        )


def row_for(table, index=1):
    return dict(zip(table[0], table[index], strict=True))


def test_latest_v5_snapshot_and_research_counters_are_exported_without_raw_data(store):
    fixture = json.loads(Path("tests/fixtures/report-research-v5.json").read_text())
    qualification = qualify(**fixture, as_of="2026-11-16")
    store.save_brief("demo-1", {"report_qualification": qualification})
    qualification["verdict"] = "hold"
    qualification["manual_checks"] = ["PRIVATE-MANUAL-CHECK"]
    qualification["passport"]["official_card_url"] = (
        "https://secret-user:secret-password@example.test/card?token=SECRET-TOKEN#SECRET-FRAGMENT"
    )
    qualification["problem_registry"][0]["name"] = "Последний перенос срока"
    store.save_brief("demo-1", {"report_qualification": qualification})
    # A later legacy snapshot must not erase the most recent v5 qualification.
    store.save_brief("demo-1", {"legacy": True})
    research(
        store,
        {
            "created_at": now(),
            "search_calls": 37,
            "page_reads": 24,
            "cache_hits": 9,
            "documents": [{"text": "RAW-CHAT-TEXT", "author": "PRIVATE-CHAT-AUTHOR"}],
            "errors": [{"detail": "PRIVATE-ERROR-CREDENTIAL"}],
            "provider_key": "SECRET-KEY",
        },
    )
    result = sheets.tables(store)
    assert set(result) == set(sheets.TABS)
    assert {
        "Реестр",
        "Неделя",
        "Девелоперы",
        "Чаты",
        "Воронка",
        "Справка",
        "Стоп-лист",
        "Заметки",
    } <= result.keys()
    passport = row_for(result["Паспорта"])
    assert passport["Решение"] == "Ручное решение"
    assert passport["Застройщик"] == qualification["passport"]["developer_brand"]
    assert passport["Официальная карточка"] == "https://example.test/card"
    assert "2027-03-31" in passport["Сроки ввода"]
    problem = row_for(result["Проблемы"])
    assert problem["Проблема"] == "Последний перенос срока"
    assert problem["Текущих за 12 месяцев"] == 2
    assert problem["Номера источников"] == "1\n2"
    covered = row_for(result["Покрытие"])
    assert covered["Поисковых запросов"] == 37
    assert covered["Прочитано страниц"] == 24
    assert covered["Попаданий в кэш"] == 9
    assert covered["Ошибок поиска"] == 1
    assert covered["Свидетельств за 12 месяцев"] == 4
    assert covered["Контуров из пяти"] == 4
    encoded = json.dumps(result)
    for private in (
        "RAW-CHAT-TEXT",
        "PRIVATE-CHAT-AUTHOR",
        "PRIVATE-ERROR-CREDENTIAL",
        "SECRET-KEY",
        "PRIVATE-MANUAL-CHECK",
        "SECRET-TOKEN",
        "secret-password",
        "secret-user",
        "SECRET-FRAGMENT",
    ):
        assert private not in encoded
    for title in ("Паспорта", "Проблемы", "Покрытие"):
        assert len(result[title]) > 1
        assert len(result[title][0]) == 16
        assert all(len(row) == 16 for row in result[title])
    assert result == sheets.tables(store)


def test_unqualified_search_has_passport_and_coverage_without_implied_negative_findings(store):
    research(
        store,
        {
            "created_at": "2026-11-16T09:00:00+00:00",
            "passport": {
                "official_name": "Новый объект",
                "developer_brand": "ГК Пример",
                "aliases": ["Пример"],
            },
            "documents": [{"contour": "official"}, {"contour": "reviews"}, {"contour": "other"}],
            "search_calls": 20,
            "page_reads": 12,
            "cache_hits": 0,
        },
    )
    result = sheets.tables(store)
    passport = row_for(result["Паспорта"])
    assert passport["ЖК"] == "Новый объект"
    assert passport["Решение"] == "Ещё не квалифицирован"
    assert passport["Проверено"] == "16.11.2026 12:00"
    covered = row_for(result["Покрытие"])
    assert covered["Контуров из пяти"] == 2
    assert covered["Чаты прочитаны"] == ""
    assert covered["Официальный срок найден"] == ""
    assert len(result["Проблемы"]) == 1


def test_empty_projection_has_all_headers_without_creating_research_table(store):
    result = sheets.tables(store)
    for title in ("Паспорта", "Проблемы", "Покрытие"):
        assert len(result[title]) == 1
    with store.connect() as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='report_research'").fetchone()
