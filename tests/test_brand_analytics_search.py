import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from shutil import copytree

import httpx
import pytest

from radar.web import brand_analytics, screening, workflow
from radar.web.discovery import Finding, Findings, SearchRequest, discover
from radar.web.sources import Page
from radar.web.store import Store, now


@pytest.fixture
def store(tmp_path):
    copytree(Path(__file__).resolve().parents[1] / "config", tmp_path / "config")
    return Store(tmp_path)


def mention(slug, text, days=1):
    return {
        "id": slug,
        "url": f"https://{slug}.example/article",
        "title": "Публикация",
        "text": text,
        "published_at": (datetime.now(UTC) - timedelta(days=days)).isoformat(),
    }


def add_record(store, ident, name, region):
    payload = {"name": name, "region": region, "developer": "ГК Эталон", "urls": []}
    with store.connect() as db:
        db.execute(
            "INSERT INTO records(id,dataset,payload,updated_at) VALUES(?,?,?,?)",
            (ident, "web", json.dumps(payload), now()),
        )


def unexpected_call(*args, **kwargs):
    pytest.fail("Local Brand Analytics matching must not use an external request")


def test_discovery_links_publications_saved_before_new_card(store):
    publication = mention(
        "right", "В Москве жители ЖК Заречье парк, корпус 3.1, обсудили выдачу ключей."
    )
    rows = [
        publication,
        mention("wrong-building", "В Москве в ЖК Заречье парк корпус 3.2 готов к заселению."),
        mention("entire-complex", "В Москве в ЖК Заречье парк жители ждут выдачу ключей."),
        mention("office", "В Москве в бизнес-центре «Заречье парк» открыли офис."),
    ]
    assert brand_analytics.ingest_messages(store, rows)["matched"] == 0
    quote = "В Москве в ЖК Заречье парк, корпус 3.1, перенесли срок выдачи ключей."
    page_url = "https://discovery.example/news"

    def reader(url):
        assert url == page_url
        return Page(
            url, url, "loaded", title="ЖК Заречье парк", text=quote,
            fetched_at=now(), content_hash="discovery-page",
        )

    prepared = {
        "documents": [{"url": page_url, "title": "ЖК Заречье парк", "text": quote}],
        "extract": lambda batch: Findings(objects=[Finding(
            name="ЖК Заречье парк", building="корпус 3.1", region="Москва",
            developer="ГК Эталон", summary=quote, quote=quote, source_index=0,
        )]),
    }
    with httpx.Client(transport=httpx.MockTransport(unexpected_call)) as client:
        result = discover(
            store, SearchRequest(region="moscow"), client, reader=reader, prepared=prepared
        )
    [ident] = result["record_ids"]
    assert result["added"] == 1 and result["external_requests"] == 0
    assert result["brand_analytics"] == {"records": 1, "publications": 1}
    assert result.get("brand_analytics_only_ids", []) == []
    [source] = brand_analytics.report_documents(store, ident)
    assert source["url"] == publication["url"]
    assert source["published_at"] == publication["published_at"]
    assert "3.1" in source["text"]


def test_rematching_rebuilds_archived_index_and_removes_ambiguous_link(store):
    add_record(store, "first", "Архитектор", "Москва")
    publication = mention(
        "namesake", "В Москве дольщики ЖК «Архитектор» ждут выдачи ключей."
    )
    brand_analytics.ingest_messages(store, [publication])
    assert len(brand_analytics.report_documents(store, "first")) == 1
    with store.connect() as db:
        db.execute("DROP TABLE brand_analytics_lookup")

    add_record(store, "second", "Архитектор", "Москва")
    brand_analytics.link_saved_messages(store, ["second"])

    assert brand_analytics.search_summary(store, ["first", "second"]) == {
        "records": 0, "publications": 0,
    }
    assert brand_analytics.status(store)["messages"] == 1


def test_discovery_returns_only_relevant_saved_cards_without_extra_research(store, monkeypatch):
    add_record(store, "fresh", "Заречье парк", "Москва")
    add_record(store, "older", "Белый квартал", "Москва")
    add_record(store, "other-region", "Светский лес", "Санкт-Петербург")
    assert brand_analytics.ingest_messages(store, [
        mention("fresh", "В Москве жители ЖК Заречье парк обсуждают выдачу ключей.", days=1),
        mention("older", "В Москве жители ЖК Белый квартал обсуждают выдачу ключей.", days=20),
        mention("other", "В Санкт-Петербурге жители ЖК Светский лес обсуждают выдачу ключей."),
    ])["matched"] == 3

    def search(period="fortnight", target=""):
        with httpx.Client(transport=httpx.MockTransport(unexpected_call)) as client:
            return discover(
                store, SearchRequest(region="moscow", period=period, target=target, enrich=True),
                client, reader=unexpected_call,
                prepared={"documents": [], "extract": unexpected_call},
            )

    result = search()
    assert result["record_ids"] == result["brand_analytics_only_ids"] == ["fresh"]
    assert result["brand_analytics"] == {"records": 1, "publications": 1}
    assert result["added"] == result["page_requests"] == result["external_requests"] == 0
    assert search(period="month")["record_ids"] == ["fresh", "older"]
    assert search(period="month", target="ЖК Белый квартал")["record_ids"] == ["older"]
    assert search(period="month", target="Светский лес")["record_ids"] == []

    def screen(store, ids, *args, **kwargs):
        assert ids == []
        return {"records": [], "status": "completed"}

    monkeypatch.setattr(screening, "screen", screen)
    enriched = workflow.complete_scan(store, result, "offline", researcher=unexpected_call)
    assert enriched["record_ids"] == ["fresh"]
    assert enriched["object_research"]["checked"] == 0
    assert enriched["brand_analytics"] == {"records": 1, "publications": 1}
