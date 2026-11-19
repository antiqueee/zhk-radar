import json
from pathlib import Path
from shutil import copytree

import pytest

from radar.web import brand_analytics, stoplist
from radar.web.store import Store, now


@pytest.fixture
def store(tmp_path):
    copytree(Path(__file__).resolve().parents[1] / "config", tmp_path / "config")
    return Store(tmp_path)


def add_record(store, ident, name, developer="Эталон", region="Москва", **extra):
    payload = {"name": name, "developer": developer, "region": region, **extra}
    with store.connect() as db:
        db.execute(
            "INSERT INTO records(id,dataset,payload,updated_at) VALUES(?,?,?,?)",
            (ident, "web", json.dumps(payload), now()),
        )


def message(slug, text, **extra):
    return {"url": f"https://news.example/{slug}", "text": text,
            "published_at": "2026-01-03T17:30:00+03:00", **extra}


def test_reimport_keeps_single_source_and_evidence_snapshot(store):
    add_record(store, "nagatino", "Nagatino I-Land", aliases=["Нагатино Ай-Лэнд"])
    raw = message(
        "nagatino?utm_source=reader", "Дольщики ЖК «Нагатино Ай-Лэнд» в Москве ждут ключи.",
        provider_id="ba-1",
    )
    assert brand_analytics.ingest_messages(store, [raw])["inserted"] == 1
    [document] = brand_analytics.report_documents(store, "nagatino")
    assert document["url"] == "https://news.example/nagatino"
    assert document["published_at"] == "2026-01-03T14:30:00+00:00"
    assert document["source_kind"] == "page" and document["read_status"] == "loaded"
    assert document["origin"] == "brand_analytics"
    result = brand_analytics.ingest_messages(store, [
        {**raw, "url": "https://news.example/nagatino/"},
        {**raw, "source_kind": "snippet", "text": "Подробности будут позже."},
        {**raw, "url": "https://other.example/incompatible"},
    ])
    assert result["unchanged"] == 2 and result["invalid"] == 1
    assert brand_analytics.report_documents(store, "nagatino") == [document]
    assert brand_analytics.status(store)["messages"] == 1


def test_only_public_dated_full_texts_can_supply_evidence(store):
    add_record(store, "nagatino", "Nagatino I-Land", aliases=["Нагатино Ай-Лэнд"])
    text = "Жители ЖК «Нагатино Ай-Лэнд» в Москве ждут ключи."
    rows = [
        message("one", text, source_kind="snippet"),
        message("two", text, published_at="2026"),
        message("three", text, truncated=True),
        message("provider", text, url="https://brandanalytics.ru/account/messages/1"),
        message("four", text, url="https://login:password@news.example/article"),
        message("five", text, url="http://127.0.0.1/article"),
        message("six", text, url="file:///tmp/article"),
    ]
    results = brand_analytics.ingest_messages(store, rows)
    assert results["inserted"] == results["matched"] == 4 and results["invalid"] == 3
    assert brand_analytics.report_documents(store, "nagatino") == []
    assert brand_analytics.ingest_messages(store, [message("one", text)])["updated"] == 1
    assert len(brand_analytics.report_documents(store, "nagatino")) == 1
    stoplist.add(store, stoplist.StopEntry(kind="record", value="nagatino"))
    assert brand_analytics.report_documents(store, "nagatino") == []


def test_disambiguates_names_and_commercial_article(store):
    add_record(store, "moscow", "Архитектор", developer="ФСК", region="Москва")
    add_record(store, "another", "Архитектор", developer="Рим", region="Ярославль")
    rows = [
        message("office", "В Москве открыли офис в бизнес-центре «Архитектор»."),
        message("ambiguous", "В ЖК «Архитектор» перенесли сроки."),
        message("hire", "В Москве ФСК наняла архитектора для строительства квартала."),
        message("moscow", "В Москве дольщики ЖК «Архитектор» от ФСК ждут выдачи ключей."),
    ]
    assert brand_analytics.ingest_messages(store, rows)["matched"] == 1
    assert [d["url"] for d in brand_analytics.report_documents(store, "moscow")] == [rows[3]["url"]]
    assert brand_analytics.report_documents(store, "another") == []


def test_exact_scope_and_multi_project_article_passages(store):
    add_record(store, "scope-31", "ЖК Заречье парк", granularity="building", building="корпус 3.1")
    add_record(store, "scope-32", "ЖК Заречье парк", granularity="building", building="корпус 3.2")
    add_record(store, "forest", "Светский лес", region="Сочи")
    rows = [
        message("complex", "Дольщики ЖК Заречье парк в Москве спрашивают о сроках."),
        message("compare", "В ЖК Заречье парк в Москве корпус 3.1 получил разрешение. "
                "В Сочи жители ЖК Светский лес обсуждают новые сроки."),
    ]
    assert brand_analytics.ingest_messages(store, rows)["matched"] == 1
    [scope] = brand_analytics.report_documents(store, "scope-31")
    assert "3.1" in scope["text"] and "Светский лес" not in scope["text"]
    [forest] = brand_analytics.report_documents(store, "forest")
    assert forest["source_id"] == scope["source_id"] and "3.1" not in forest["text"]
    assert brand_analytics.report_documents(store, "scope-32") == []
