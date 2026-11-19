import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from radar.web import report_search
from radar.web.sources import Page
from radar.web.store import Store

_TITLE = "ЖК Северный берег, корпус 3, Московская область"


@pytest.fixture
def store(tmp_path, monkeypatch):
    shutil.copytree(Path("config"), tmp_path / "config")
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "YANDEX_SEARCH_API_KEY=test\nYANDEX_FOLDER_ID=test\n"
        "OPENROUTER_API_KEY=test\nPERPLEXITY_API_KEY=test\n"
    )
    monkeypatch.setattr("radar.web.workflow.reserve_call", lambda *args: None)
    plan = report_search.search_config()
    plan["query_schedule"]["first_pass_limit"] = 3
    plan["budget"]["search_calls"] = 5
    plan["followup"]["max_rounds"] = 0
    monkeypatch.setattr(report_search, "search_config", lambda: plan)
    store = Store(tmp_path)
    with store.connect() as db:
        db.execute("UPDATE records SET dataset='web' WHERE id='demo-1'")
    return store


def _candidate(url):
    return {"url": url, "title": _TITLE, "text": "Дольщики обсуждают передачу ключей"}


def _loaded(url):
    return Page(
        url, url, "loaded", title=_TITLE, text="Жильцы третьего корпуса обсуждают ключи.",
        published_at="2026-11-01", fetched_at=datetime.now(UTC).isoformat(),
    )


def _ensure(store, searcher, perplexity_searcher, reader=_loaded):
    return report_search.ensure_research(
        store, "demo-1", "run", object(), searcher=searcher,
        perplexity_searcher=perplexity_searcher, reader=reader,
        entity_finder=lambda *args: [], today=date(2026, 11, 16),
    )


def test_sparse_report_adds_perplexity_with_same_budget_and_original_sources(store):
    requests, reads = [], []
    already = "https://msk1.ru/known"
    new = "https://cian.ru/review"
    bad = "https://cian.ru/other-object"

    def yandex(*args):
        requests.append("yandex")
        return [_candidate(already)]

    def perplexity(client, cfg, query, period):
        requests.append("perplexity")
        assert "Московская область" in query and "корпус 3" in query and period == "all"
        return [_candidate(already + "/?ysclid=tracker"), _candidate(new), _candidate(bad)]

    def read(url):
        reads.append(url)
        page = _loaded(url)
        if url == bad:
            page.title = "Другое место"
            page.text = "Сведения о другом строительстве."
        return page

    result = _ensure(store, yandex, perplexity, read)
    assert result["completed"] and result["search_calls"] == 5
    assert requests == ["yandex", "yandex", "yandex", "perplexity", "perplexity"]
    assert result["perplexity"] == {"queries": 2, "new_urls": 2, "errors": []}
    assert sorted(reads) == sorted([already, new, bad]) and len(reads) == 3
    documents = {d["url"]: d for d in result["documents"]}
    assert "search_provider" not in documents[already]
    assert documents[new]["search_provider"] == "perplexity"
    assert documents[new]["published_at"] == "2026-11-01"
    assert documents[new]["search_snippet"] != documents[new]["text"]
    assert documents[bad]["read_status"] == "out_of_scope" and documents[bad]["text"] == ""


def test_secondary_failure_preserves_completed_report_and_remaining_yandex_slot(store):
    calls = []

    def yandex(*args):
        calls.append("yandex")
        return [_candidate("https://msk1.ru/known")]

    def perplexity(*args):
        calls.append("perplexity")
        raise ValueError("Perplexity Search: ответ 401")

    result = _ensure(store, yandex, perplexity)
    assert result["completed"] and not result["errors"]
    assert calls == ["yandex", "yandex", "yandex", "perplexity", "yandex"]
    assert result["perplexity"]["errors"] == ["Perplexity Search: ответ 401"]
    assert result["documents"][0]["read_status"] == "loaded"


def test_report_with_diverse_loaded_coverage_skips_perplexity(store):
    domains = ["msk1.ru", "cian.ru", "novostroev.ru", "mos.ru"]
    result = _ensure(
        store, lambda *args: [_candidate(f"https://{domain}/article") for domain in domains],
        lambda *args: pytest.fail("Loaded sources cover enough independent publishers"),
    )
    assert result["perplexity"] == {"queries": 0, "new_urls": 0, "errors": []}
    assert len(result["documents"]) == len(domains)


def test_configuring_perplexity_reuses_yandex_cache_and_caches_supplement(store):
    dotenv = store.root / ".env"
    dotenv.write_text(dotenv.read_text().replace("PERPLEXITY_API_KEY=test\n", ""))
    prior = _ensure(
        store, lambda *args: [_candidate("https://msk1.ru/known")],
        lambda *args: pytest.fail("No Perplexity configured"),
    )
    assert "perplexity" not in prior
    dotenv.write_text(dotenv.read_text() + "PERPLEXITY_API_KEY=test\n")

    def no_yandex(*args):
        pytest.fail("Yandex query should use its existing cache")

    refreshed = _ensure(
        store, no_yandex,
        lambda *args: [_candidate("https://cian.ru/exclusive")],
    )
    assert refreshed["signature"] != prior["signature"]
    assert refreshed["perplexity"]["new_urls"] == 1
    reused = _ensure(
        store, no_yandex, lambda *args: pytest.fail("Perplexity result should use its cache")
    )
    assert reused["cached"] and reused["documents"] == refreshed["documents"]
