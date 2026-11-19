import json
from pathlib import Path
from shutil import copytree

import httpx
import pytest
import yaml

from radar.web import discovery, report_search
from radar.web.sources import Page
from radar.web.store import Store, now

PRIMARY = "https://msk1.ru/known"
DIRECT = "https://cian.ru/direct"
NATIVE = "https://cian.ru/native"
TITLE = "ЖК Северный берег, корпус 3, Московская область"
TEXT = "Жильцы третьего корпуса обсуждают сроки передачи ключей."
MODEL = "perplexity/sonar"
OTHER_MODEL = "perplexity/sonar-pro"


def _set_model(store, model):
    path = store.root / "config/models.yaml"
    config = yaml.safe_load(path.read_text())
    config["perplexity_search"]["model"] = model
    path.write_text(yaml.safe_dump(config, allow_unicode=True))


@pytest.fixture
def store(tmp_path, monkeypatch):
    copytree(Path(__file__).resolve().parents[1] / "config", tmp_path / "config")
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "YANDEX_SEARCH_API_KEY=test\nYANDEX_FOLDER_ID=test\nOPENROUTER_API_KEY=gateway-token\n"
    )
    store = Store(tmp_path)
    _set_model(store, "")
    with store.connect() as db:
        db.execute("UPDATE records SET dataset='web' WHERE id='demo-1'")
    return store


def _page(url, title=TITLE):
    return Page(url, url, "loaded", title=title, text=TEXT, fetched_at=now())


def _sonar_response(*urls):
    return httpx.Response(200, json={
        "usage": {"cost": 0.007},
        "choices": [{"message": {"annotations": [
            {"type": "url_citation", "url_citation": {"url": url}} for url in urls
        ]}}],
    })


def test_discovery_switches_from_direct_to_gateway_and_caches_by_model(store, monkeypatch):
    monkeypatch.setenv("PERPLEXITY_API_KEY", "direct-token")
    providers = []
    primary_calls = []

    def respond(request):
        if request.url.host == "api.perplexity.ai":
            assert request.headers["Authorization"] == "Bearer direct-token"
            providers.append("direct")
            return httpx.Response(200, json={"results": [{"url": DIRECT}]})
        assert request.url.host == "openrouter.ai"
        assert request.headers["Authorization"] == "Bearer gateway-token"
        providers.append(json.loads(request.content)["model"])
        return _sonar_response(NATIVE)

    def yandex(client, cfg, query, period):
        primary_calls.append(query)
        return [{"url": PRIMARY, "title": TITLE, "text": TEXT, "query": query}]

    monkeypatch.setattr(discovery, "yandex", yandex)
    monkeypatch.setattr(discovery, "extract", lambda *args: discovery.Findings(objects=[]))
    request = discovery.SearchRequest(region="moscow", target=TITLE)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        first = discovery.discover(store, request, client, reader=_page)
        assert first["perplexity"]["read_urls"] == [DIRECT]
        _set_model(store, MODEL)
        switched = discovery.discover(store, request, client, reader=_page)
        assert switched["perplexity"]["read_urls"] == [NATIVE]
        assert discovery.discover(store, request, client, reader=_page)["cached"]
        _set_model(store, OTHER_MODEL)
        discovery.discover(store, request, client, reader=_page)

    assert len(primary_calls) == 4
    assert providers == ["direct", "direct", MODEL, MODEL, OTHER_MODEL, OTHER_MODEL]
    with store.connect() as db:
        costs = db.execute(
            "SELECT billed_usd FROM api_calls WHERE service='openrouter_perplexity'"
        ).fetchall()
    assert [row[0] for row in costs] == [0.007] * 4


def test_report_checks_only_two_unanchored_gateway_citations_on_the_page(store, monkeypatch):
    _set_model(store, MODEL)
    wrong = "https://novostroev.ru/wrong"
    over_limit = "https://m24.ru/over-limit"
    responses = []
    reads = []
    primary_calls = []
    plan = report_search.search_config()
    plan["query_schedule"]["first_pass_limit"] = 3
    plan["budget"]["search_calls"] = 5
    plan["followup"]["max_rounds"] = 0
    monkeypatch.setattr(report_search, "search_config", lambda: plan)

    def respond(request):
        assert request.url.host == "openrouter.ai"
        responses.append(json.loads(request.content)["model"])
        return _sonar_response(NATIVE, wrong, over_limit)

    def read(url):
        reads.append(url)
        return _page(url, title="Другой объект" if url == wrong else TITLE)

    def yandex(client, cfg, query, period):
        primary_calls.append(query)
        return [{"url": PRIMARY, "title": TITLE, "text": TEXT}]

    def research(client):
        return report_search.ensure_research(
            store, "demo-1", "fixture", client, searcher=yandex, reader=read,
            entity_finder=lambda *args: [],
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = research(client)
        assert research(client)["cached"]
        _set_model(store, OTHER_MODEL)
        refreshed = research(client)

    assert refreshed["signature"] != result["signature"]
    assert responses == [MODEL, MODEL, OTHER_MODEL, OTHER_MODEL]
    assert len(primary_calls) == 3
    assert result["completed"] and result["search_calls"] == 5
    assert set(reads) == {PRIMARY, NATIVE, wrong}
    documents = {row["url"]: row for row in result["documents"]}
    assert documents[NATIVE]["read_status"] == "loaded"
    assert documents[NATIVE]["search_provider"] == "perplexity"
    assert documents[wrong]["read_status"] == "out_of_scope"
    assert documents[wrong]["text"] == ""
