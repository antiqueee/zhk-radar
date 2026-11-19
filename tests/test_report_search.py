import json
import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import yaml

from radar.web import report_search
from radar.web.evidence_context import sources_for
from radar.web.sources import Page
from radar.web.store import Store


@pytest.fixture
def store(tmp_path, monkeypatch):
    shutil.copytree(Path("config"), tmp_path / "config")
    (tmp_path / ".env").write_text(
        "YANDEX_SEARCH_API_KEY=test\nYANDEX_FOLDER_ID=test\nOPENROUTER_API_KEY=test\n"
    )
    monkeypatch.setattr("radar.web.workflow.reserve_call", lambda *a: None)
    store = Store(tmp_path)
    with store.connect() as db:
        db.execute("UPDATE records SET dataset='web' WHERE id='demo-1'")
    return store


def test_query_plan_covers_every_contour_and_alias():
    record = {"name": "Светлый мир «Станция Л»", "region": "Ленинградская область"}
    aliases = report_search.aliases_for(record)
    plan = report_search.query_plan(record, aliases, date(2026, 11, 16))
    assert "Станция Л" in aliases
    assert {q["contour"] for q in plan} == set(report_search.CONTOURS)
    assert all(any(f'"{a}"' in q["query"] for q in plan) for a in aliases)
    assert any(q["period"] == "month" for q in plan)
    assert any("date:>=20251116" in q["query"] for q in plan)
    assert len(plan) <= 24


def test_two_pass_research_budgets_dates_full_text_and_cache(store):
    searches, reads = [], []
    domains = ["erzrf.ru", "msk1.ru", "cian.ru", "t.me", "mos.ru"]

    def searcher(client, cfg, query, period):
        searches.append((query, period))
        index = len(searches)
        return [
            {
                "url": f"https://{domain}/source/{index}",
                "title": "ЖК Северный берег, Московская область",
                "text": "Короткий текст о Северном береге, Демо Девелопмент",
            }
            for domain in domains
        ]

    def reader(url):
        reads.append(url)
        return Page(
            url,
            url,
            "loaded",
            title="Хронология ЖК Северный берег, Московская область",
            text="палаточный лагерь в ЖК Северный берег. " + "Полный текст события. " * 1500,
            published_at="2026-11-01",
            fetched_at=datetime.now(UTC).isoformat(),
        )

    def entities(*args):
        return [report_search.Entity(text="палаточный лагерь", source_url=reads[0], kind="event")]

    with httpx.Client(
        transport=httpx.MockTransport(lambda request: pytest.fail("Unexpected network"))
    ) as client:
        result = report_search.ensure_research(
            store,
            "demo-1",
            "run",
            client,
            searcher=searcher,
            reader=reader,
            entity_finder=entities,
            today=date(2026, 11, 16),
        )
        again = report_search.ensure_research(
            store,
            "demo-1",
            "run2",
            client,
            searcher=lambda *a: pytest.fail("Repeated paid search"),
            reader=lambda *a: pytest.fail("Repeated page read"),
            entity_finder=lambda *a: pytest.fail("Repeated entity call"),
        )
    assert again["cached"] is True
    assert result["passes"] == 2
    assert 24 <= len(searches) <= 40
    expected_reads = result["budget"]["total_page_reads"] - result["budget"]["reserved_page_reads"]
    assert len(reads) == result["page_reads"] == expected_reads == 21
    assert len(reads) == len(set(reads))
    first_pass = len(
        report_search.query_plan(
            store.get("demo-1"), report_search.aliases_for(store.get("demo-1")), date(2026, 11, 16)
        )
    )
    assert any("палаточный лагерь" in query for query, _ in searches[first_pass:])
    assert any(int(url.rsplit("/", 1)[-1]) > first_pass for url in reads[16:])
    assert {d["contour"] for d in result["documents"]} == set(report_search.CONTOURS)
    assert all(d["published_at"] == "2026-11-01" for d in result["documents"])
    assert all(len(d["text"]) > 20000 for d in result["documents"])
    assert report_search.get_research(store, "demo-1")["documents"] == result["documents"]
    payload = {"report_research": result}
    assert len(sources_for(payload)) == expected_reads


def test_community_hostname_is_not_inferred_from_path_or_lookalike():
    assert report_search.contour_for("https://t.me/project/12") == "communities"
    assert report_search.contour_for("https://t.me.evil.example/project") == "other"
    assert report_search.contour_for("https://example.org/t.me") == "other"
    assert report_search.contour_for("https://xn--80az8a.xn--d1aqf.xn--p1ai/1") == "official"


def test_research_snippet_does_not_invent_publication_date(store):
    def searcher(*args):
        return [
            {
                "url": "https://cian.ru/project/1",
                "text": "Срок перенесён",
                "title": "ЖК Северный берег, Московская область",
            }
        ]

    def reader(url):
        return Page(url, url, "unavailable", fetched_at=datetime.now(UTC).isoformat())

    result = report_search.ensure_research(
        store,
        "demo-1",
        "snippets",
        object(),
        searcher=searcher,
        reader=reader,
        entity_finder=lambda *a: [],
    )
    doc = result["documents"][0]
    assert doc["source_kind"] == "snippet"
    assert doc["text"] == "" and doc["search_snippet"] == "Срок перенесён"
    assert sources_for({"report_research": result}) == []
    assert doc["published_at"] is None
    assert doc["checked_at"]
    assert json.loads(json.dumps(result))["documents"][0]["published_at"] is None


def test_public_preview_uses_one_read_slot_and_only_actual_messages_claim_chat(store):
    reads = []
    messages = [
        {
            "url": "https://t.me/severny_chat/11",
            "date": "2026-11-15T10:00:00Z",
            "text": "Ключи перенесены.",
        },
        {
            "url": "https://t.me/severny_chat/12",
            "date": "2026-11-16T10:00:00Z",
            "text": "Ждём встречу.",
        },
    ]

    def searcher(*args):
        return [
            {
                "url": "https://t.me/severny_chat",
                "title": "Чат ЖК Северный берег, Московская область",
            },
            {"url": "https://t.me/s/severny_chat", "title": "Публичный Telegram"},
            {"url": "https://t.me/+privateinvite", "title": "Чат ЖК Северный"},
            {"url": "https://t.me/ivan_petrov", "title": "Telegram: Contact @ivan_petrov"},
            {"url": "https://t.me/severny_bot", "title": "Telegram"},
            {
                "url": "https://vk.com/severny_chat",
                "title": "Группа ЖК Северный берег, Московская область",
            },
        ]

    def reader(url):
        reads.append(url)
        return Page(
            url,
            url,
            "loaded",
            title="ЖК Северный берег, Московская область",
            text="Ключи перенесены. Ждём встречу.",
            messages=messages if url.startswith("https://t.me/s/") else None,
            fetched_at=datetime.now(UTC).isoformat(),
        )

    result = report_search.ensure_research(
        store,
        "demo-1",
        "public-preview",
        object(),
        searcher=searcher,
        reader=reader,
        entity_finder=lambda *a: [],
    )
    assert sorted(reads) == ["https://t.me/s/severny_chat", "https://vk.com/severny_chat"]
    assert result["page_reads"] == 2
    sources = sources_for({"report_research": result})
    source = next(item for item in sources if item["kind"] == "chat")
    landing = next(item for item in sources if item["kind"] == "page")
    assert source["kind"] == "chat" and source["count"] == 2
    assert source["messages"] == messages
    assert source["message_urls"] == [message["url"] for message in messages]
    assert source["message_dates"] == [message["date"] for message in messages]
    assert source["published_at"] is None
    assert landing["kind"] == "page" and "messages" not in landing


def configured_plan(tmp_path, monkeypatch):
    plan = report_search.search_config()
    path = tmp_path / "report-search.yaml"
    path.write_text(yaml.safe_dump(plan, allow_unicode=True))
    monkeypatch.setattr(report_search, "CONFIG", path)
    return plan, path


def test_yaml_controls_terms_recent_lexicon_domains_and_second_history(tmp_path, monkeypatch):
    plan, path = configured_plan(tmp_path, monkeypatch)
    plan["query_schedule"]["terms"]["official"] = {"term": "особый срок"}
    plan["query_schedule"]["recent_terms"]["media"] = {"term": "особая жалоба"}
    plan["query_schedule"]["domains"]["reviews"] = ["new-reviews.example"]
    plan["lexicon"]["деньги"] = ["неоплаченная компенсация"]
    plan["query_schedule"]["second_history"] = [{"term": "уточнённая хронология"}]
    path.write_text(yaml.safe_dump(plan, allow_unicode=True, sort_keys=False))
    record = {"name": "ЖК Образец", "region": "Казань"}
    first = report_search.query_plan(record, [record["name"]], date(2026, 11, 16))
    assert any("особый срок" in q["query"] and q["contour"] == "official" for q in first)
    assert any("особая жалоба" in q["query"] and q["period"] == "month" for q in first)
    assert any("site:new-reviews.example" in q["query"] for q in first)
    assert any("неоплаченная компенсация" in q["query"] for q in first)
    assert report_search.contour_for("https://new-reviews.example/project") == "reviews"
    second = report_search.second_pass_plan(record, [record["name"]], [])
    assert len(second) == 1
    assert "уточнённая хронология" in second[0]["query"]
    assert len(first) <= 24 and len(first) + len(second) <= 40


def test_each_lexicon_family_has_one_intent_without_and_overload(tmp_path, monkeypatch):
    plan, path = configured_plan(tmp_path, monkeypatch)
    # Distinct one-word probes make omissions and concatenated intentions observable.
    probes = {group: f"тема{index}" for index, group in enumerate(plan["lexicon"])}
    for group, probe in probes.items():
        plan["lexicon"][group] = [probe]
    path.write_text(yaml.safe_dump(plan, allow_unicode=True, sort_keys=False))
    record = {"name": "ЖК Образец", "region": "Казань"}
    aliases = ["Образец полный", "Образец старый", "Образец очередь", "Образец"]
    first = report_search.query_plan(record, aliases, date(2026, 11, 16))
    assert all(any(probe in q["query"] for q in first) for probe in probes.values())
    assert all(sum(probe in q["query"] for probe in probes.values()) <= 1 for q in first)
    assert all(any(f'"{alias}"' in q["query"] for q in first) for alias in aliases)
    for contour in report_search.CONTOURS:
        assert any(q["contour"] == contour and "site:" in q["query"] for q in first)
    assert len(first) <= 24


def test_full_vocabulary_rotates_weekly_within_the_same_query_budget():
    plan = report_search.search_config()
    record = {"name": "ЖК Образец", "region": "Казань"}
    selected = {
        report_search._query_term(
            {"group": "деньги"},
            plan,
            record,
            date(2026, 1, 1) + report_search.timedelta(days=7 * n),
        )
        for n in range(len(plan["lexicon"]["деньги"]))
    }
    assert selected == set(plan["lexicon"]["деньги"])


def test_yaml_change_replans_cached_research_without_exceeding_hard_limits(
    store, tmp_path, monkeypatch
):
    plan, path = configured_plan(tmp_path, monkeypatch)
    plan["budget"].update(search_calls=500, page_reads=500)
    plan["query_schedule"].update(first_pass_limit=500, second_pass_limit=500)
    path.write_text(yaml.safe_dump(plan, allow_unicode=True, sort_keys=False))
    searches, reads = [], []

    def searcher(client, cfg, query, period):
        searches.append(query)
        return [
            {
                "url": f"https://msk1.ru/source/{len(searches)}",
                "text": "Материал",
                "title": "Источник",
            }
        ]

    def reader(url):
        reads.append(url)
        return Page(url, url, "loaded", text="Материал", fetched_at=datetime.now(UTC).isoformat())

    entities = [
        report_search.Entity(
            text=f"Компания номер {i}", source_url="https://msk1.ru/source/1", kind="organization"
        )
        for i in range(30)
    ]
    kwargs = {"searcher": searcher, "reader": reader, "entity_finder": lambda *a: entities}
    first = report_search.ensure_research(store, "demo-1", "first", object(), **kwargs)
    assert first["search_calls"] <= 40 and first["page_reads"] <= 25
    assert len(searches) <= 40 and len(reads) <= 25
    plan["query_schedule"]["terms"]["official"] = {"term": "новый ориентир"}
    path.write_text(yaml.safe_dump(plan, allow_unicode=True, sort_keys=False))
    again = report_search.ensure_research(store, "demo-1", "changed", object(), **kwargs)
    assert again["signature"] != first["signature"]
    assert not again.get("cached")
    assert any("новый ориентир" in query for query in searches)
    assert again["search_calls"] <= 40 and again["page_reads"] <= 25


def test_primary_text_version_rebuilds_from_raw_page_cache_without_paid_search(
    store, monkeypatch
):
    from radar.web.sources import primary_text

    url = "https://news.mondiara.com/categories/108/posts/265038"
    main = "ЖК Северный берег, Московская область. Завершена отделка корпуса 3."
    related = "Корпус 4 другого проекта перенесён."
    searches, reads = [], []

    def searcher(*args):
        searches.append(1)
        return [{"url": url, "title": "ЖК Северный берег, Московская область", "text": main}]

    def reader(requested):
        reads.append(requested)
        return Page(
            requested, requested, "loaded", title="ЖК Северный берег, Московская область",
            text=main + "\nДругие новости сообщества\n" + related,
            published_at="2026-11-06", fetched_at=datetime.now(UTC).isoformat(),
        )

    kwargs = dict(
        searcher=searcher, reader=reader, entity_finder=lambda *args: [],
        today=date(2026, 11, 17),
    )
    monkeypatch.setattr(report_search, "PRIMARY_TEXT_VERSION", "old-boundaries")
    monkeypatch.setattr(report_search, "primary_text", lambda url, text: text)
    first = report_search.ensure_research(store, "demo-1", "first", object(), **kwargs)
    assert related in first["documents"][0]["text"]
    first_search_count = len(searches)
    monkeypatch.setattr(report_search, "PRIMARY_TEXT_VERSION", "new-boundaries")
    monkeypatch.setattr(report_search, "primary_text", primary_text)
    again = report_search.ensure_research(store, "demo-1", "second", object(), **kwargs)
    assert first["signature"] != again["signature"]
    assert again["documents"][0]["text"] == main
    assert len(reads) == 1 and len(searches) == first_search_count
    with store.connect() as db:
        raw = json.loads(db.execute("SELECT payload FROM source_pages").fetchone()[0])
    assert related in raw["text"]
