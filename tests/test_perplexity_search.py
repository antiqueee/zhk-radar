import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from shutil import copytree

import httpx
import pytest

from radar.web.benchmark import search_provider
from radar.web.money import PERPLEXITY_REQUEST_USD
from radar.web.operations import reserve_call
from radar.web.perplexity_search import search_perplexity
from radar.web.store import Store

_CONFIG = {"PERPLEXITY_API_KEY": "fixture-search-token"}


@pytest.mark.parametrize("period", ["month", "fortnight", "all"])
def test_search_filters_and_canonical_results(period):
    def respond(request):
        assert str(request.url) == "https://api.perplexity.ai/search"
        assert request.headers["Authorization"] == "Bearer fixture-search-token"
        payload = json.loads(request.content)
        assert payload["country"] == "RU" and payload["search_language_filter"] == ["ru"]
        if period == "month":
            assert payload["search_recency_filter"] == "month"
        else:
            assert "search_recency_filter" not in payload
        if period == "fortnight":
            expected = datetime.now(UTC).date() - timedelta(days=14)
            assert payload["search_after_date_filter"] == expected.strftime("%m/%d/%Y")
        else:
            assert "search_after_date_filter" not in payload
        return httpx.Response(200, json={"results": [
            {"url": "https://news.example/project?utm_campaign=example", "title": "Публикация",
             "snippet": "Обсуждение сроков проекта.", "date": "2026-11-18"},
            {"url": "https://news.example/project", "title": "Та же публикация"},
            {"url": "file:///tmp/invalid", "title": "Недопустимый адрес"},
            {"url": {}},
        ]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        [row] = search_perplexity(client, _CONFIG, "ЖК Заречье", period)
    assert row == {
        "url": "https://news.example/project", "title": "Публикация",
        "text": "Обсуждение сроков проекта.", "query": "ЖК Заречье",
    }


@pytest.mark.parametrize("service", ["perplexity_search", "benchmark:perplexity"])
def test_successful_search_sets_fixed_search_api_cost(tmp_path, service):
    copytree(Path(__file__).resolve().parents[1] / "config", tmp_path / "config")
    store = Store(tmp_path)
    ticket = reserve_call(store, "perplexity-run", service)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"results": []}))
    with httpx.Client(transport=transport) as client:
        assert search_provider(client, _CONFIG, "perplexity", "ЖК Заречье") == []
    with store.connect() as db:
        row = db.execute(
            "SELECT reserved_usd,billed_usd FROM api_calls WHERE id=?", (ticket,),
        ).fetchone()
    assert tuple(row) == (PERPLEXITY_REQUEST_USD, PERPLEXITY_REQUEST_USD)
