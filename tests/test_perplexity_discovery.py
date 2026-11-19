import shutil
from pathlib import Path

import httpx
import pytest

from radar.web import discovery
from radar.web.configuration import SettingsUpdate, save_settings
from radar.web.operations import OperationsSettings, save, usage
from radar.web.sources import Page
from radar.web.store import Store, now

PRIMARY_URL = "https://news.example/primary"
NEW_URL = "https://forum.example/supplement"
QUOTE = "Дольщики ЖК Северный сообщают о переносе срока в Москве."


def _result(url, query):
    return {"url": url, "title": "ЖК Северный", "text": "Короткий фрагмент", "query": query}


@pytest.fixture
def scan(tmp_path, monkeypatch):
    shutil.copytree(Path("config"), tmp_path / "config")
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    save_settings(
        tmp_path,
        SettingsUpdate(
            values={
                "YANDEX_SEARCH_API_KEY": "test-search",
                "YANDEX_FOLDER_ID": "test-folder",
                "OPENROUTER_API_KEY": "test-model",
                "PERPLEXITY_API_KEY": "test-perplexity",
            },
            model="test/extractor",
        ),
    )
    pages, extracted = [], []

    def reader(url):
        pages.append(url)
        return Page(
            url, url, "loaded", title="ЖК Северный", text=QUOTE, fetched_at=now(),
            content_hash=url,
        )

    def extract(client, cfg, model, docs):
        extracted.extend(docs)
        return discovery.Findings(objects=[
            discovery.Finding(
                name="ЖК Северный", region="Москва", summary=QUOTE,
                source_index=index, quote=QUOTE,
            )
            for index, doc in enumerate(docs) if doc["url"] == NEW_URL
        ])

    def unexpected_http(request):
        pytest.fail(f"Unexpected external request: {request.url.host}")

    monkeypatch.setattr(discovery, "extract", extract)
    monkeypatch.setattr(
        discovery, "yandex", lambda client, cfg, query, period: [_result(PRIMARY_URL, query)]
    )
    store = Store(tmp_path)

    def run(page_reader=reader):
        with httpx.Client(transport=httpx.MockTransport(unexpected_http)) as client:
            return discovery.discover(
                store, discovery.SearchRequest(region="moscow", target="ЖК Северный"),
                client, reader=page_reader,
            )

    return run, store, pages, extracted


def test_supplement_deduplicates_limits_and_reuses_results(scan, monkeypatch):
    run, store, pages, extracted = scan
    calls = []

    def perplexity(client, cfg, query, period):
        calls.append((query, period))
        return [_result(PRIMARY_URL + "?utm_source=perplexity", query), _result(NEW_URL, query)]

    monkeypatch.setattr(discovery, "search_perplexity", perplexity)
    result = run()
    assert len(calls) == 2 and all(period == "all" for _, period in calls)
    assert pages == [PRIMARY_URL, NEW_URL]
    assert extracted[1]["search_provider"] == "perplexity"
    assert extracted[1]["text"] == QUOTE
    assert result["found"] == 1
    assert result["perplexity"] == {
        "queries_completed": 2, "unique_urls": 1, "full_pages": 1,
        "read_urls": [NEW_URL], "errors": [],
    }
    assert run()["cached"]
    assert len(calls) == 2 and pages == [PRIMARY_URL, NEW_URL]
    billed = {row["service"]: row["calls"] for row in usage(store)["services"]}
    assert billed["perplexity_search"] == 2


def test_optional_failure_keeps_primary_pages_and_hides_provider_details(scan, monkeypatch):
    run, _, pages, _ = scan

    def unavailable(*args):
        raise ValueError("private API key and provider response")

    monkeypatch.setattr(discovery, "search_perplexity", unavailable)
    result = run()
    assert pages == [PRIMARY_URL]
    assert result["status"] == "completed"
    assert len(result["perplexity"]["errors"]) == 2
    assert "private API key" not in str(result)


def test_fallback_after_primary_auth_failure(scan, monkeypatch):
    run, _, pages, _ = scan

    def unavailable(*args):
        raise ValueError("Yandex Search: ответ 401.")

    monkeypatch.setattr(discovery, "yandex", unavailable)
    monkeypatch.setattr(
        discovery, "search_perplexity",
        lambda client, cfg, query, period: [_result(NEW_URL, query)],
    )
    result = run()
    assert pages == [NEW_URL]
    assert result["found"] == 1 and result["status"] == "partial"
    assert result["coverage"]["queries_completed"] == 0
    assert result["perplexity"]["full_pages"] == 1


def test_supplement_obeys_shared_search_allowance(scan, monkeypatch):
    run, store, pages, _ = scan
    save(store, OperationsSettings(per_run=4))

    def unexpected(*args):
        pytest.fail("Perplexity must not spend the extract budget")

    monkeypatch.setattr(discovery, "search_perplexity", unexpected)
    result = run()
    assert pages == [PRIMARY_URL]
    assert result["perplexity"]["queries_completed"] == 0
    assert result["perplexity"]["errors"]


def test_supplement_skips_queries_with_good_coverage(scan, monkeypatch):
    run, _, pages, _ = scan

    def primary(client, cfg, query, period):
        group = query.replace(" ", "")
        return [_result(f"https://news.example/{group}/{i}", query) for i in range(3)]

    def unexpected(*args):
        pytest.fail("Perplexity should only supplement sparse results")

    monkeypatch.setattr(discovery, "yandex", primary)
    monkeypatch.setattr(discovery, "search_perplexity", unexpected)
    result = run()
    assert "perplexity" not in result and len(pages) == 12


def test_perplexity_snippet_is_not_sent_to_extractor_when_page_fails(scan, monkeypatch):
    run, _, _, extracted = scan

    def reader(url):
        if url == NEW_URL:
            return Page(url, url, "error", error="Страница недоступна", fetched_at=now())
        return Page(
            url, url, "loaded", title="ЖК Северный", text=QUOTE, fetched_at=now(),
            content_hash=url,
        )

    monkeypatch.setattr(
        discovery, "search_perplexity",
        lambda client, cfg, query, period: [{**_result(NEW_URL, query), "text": QUOTE}],
    )
    result = run(reader)
    assert [doc["url"] for doc in extracted] == [PRIMARY_URL]
    assert result["found"] == 0
    assert result["perplexity"]["full_pages"] == 0
    assert result["perplexity"]["read_urls"] == []
