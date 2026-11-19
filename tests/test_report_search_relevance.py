import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from radar.web import report_search
from radar.web.sources import Page, load_page
from radar.web.store import Store

KROST = {
    "name": "Новая Звезда — II очередь",
    "developer": "КРОСТ",
    "region": "Москва",
    "address": "Коммунарка, ул. Бачуринская",
    "aliases": ["Новая Звезда 2", "Новая Звезда II очередь", "Новая Звезда"],
    "official_domains": ["krost.ru"],
    "report_scope": {
        "house_id": "66399",
        "exclude_locations": ["Расплетина 14", "I очередь сдана"],
    },
}


def test_read_slots_prefer_undercovered_contours_and_new_publishers():
    def page(url, contour, window="30d", **fields):
        return {"url": url, "contour": contour, "window": window,
                "relevance": {"score": 8}, **fields}

    previous = {
        "https://media.example/one": page(
            "https://media.example/one", "media", read_status="loaded", text="Материал"
        ),
        "https://unavailable.example/one": page(
            "https://unavailable.example/one", "authorities", read_status="unavailable"
        ),
    }
    seed = page("https://krost.ru/project", "official", "primary", operator_seed=True)
    authority = page("https://mos.ru/news/project", "authorities", "12m")
    repeat = page("https://www.media.example/two", "media")
    independent = page("https://other.example/article", "media", "12m")
    rows = [*previous.values(), repeat, independent, seed, authority]
    candidates = {row["url"]: row for row in rows}
    picked = report_search.select_read_candidates(candidates, previous, 3, {repeat["url"]})
    assert picked == [seed, authority, independent]
    assert report_search.select_read_candidates(
        candidates, previous, 2, recently_unavailable={authority["url"]}
    ) == [seed, independent]


def test_business_center_with_same_name_is_not_a_residential_source():
    record = {"name": "Nagatino I-Land", "aliases": ["Нагатино Айленд"], "region": "Москва"}
    news = {"url": "https://realty.ria.ru/news/article", "title": "Открыт новый офис",
            "text": "Компания арендовала офис в бизнес-центре «Нагатино Айленд» в Москве."}
    assert report_search.candidate_relevance(record, news)["reason"] == "commercial_entity"
    news["text"] += " Рядом строится ЖК «Нагатино Айленд», застройщик уточнил срок."
    assert report_search.candidate_relevance(record, news)["accepted"] is True


BAD = [
    {
        "url": "https://spacemuz.org/album/various-artists-novaya-zvezda-2020-luchshie-pesni",
        "title": "Новая Звезда 2020. Лучшие песни — Various Artists — треки онлайн и скачать mp3",
        "text": "Новая Звезда 2020, музыка в Москве",
    },
    {
        "url": "https://strmk.ru/novaya-zvezda-2.html",
        "title": "Новая звезда Ⅱ — стремянка",
        "text": "Новая звезда, стремянка для дома",
    },
    {
        "url": "https://realty.ria.ru/20210602/biatlon-1735261530.html",
        "title": "Центр биатлона в Митино планируют открыть в августе — Недвижимость РИА Новости",
        "text": "КРОСТ строит ЖК Новая Звезда в Москве — ссылки на другие новости компании",
    },
    {
        "url": "https://msk.rt.ru/homeinternet/check-address/complex-novaya-zvezda-2",
        "title": "Интернет-провайдер в Новая Звезда-2 в Москве | Тарифы от 650 ₽",
        "text": "Подключить интернет, Новая Звезда-2",
    },
    {
        "url": "https://example.org/raspletina-14",
        "title": "ЖК Новая Звезда, Расплетина 14",
        "text": "КРОСТ Москва",
    },
    {
        "url": "https://example.org/another-city",
        "title": "ЖК Новая Звезда в Петербурге",
        "text": "Застройщик и другой проект в Санкт-Петербурге",
    },
    {
        "url": "https://krost.ru/news/other-project",
        "title": "ЖК «Трилогия Кристалл»",
        "text": "КРОСТ, Москва, другой жилой комплекс. В блоке других новостей: "
        "Новая Звезда II очередь.",
    },
]
GOOD = [
    {
        "url": "https://www.novostroy-m.ru/baza/jk_novaya_zvezda_premium",
        "title": "ЖК Новая Звезда II очередь — Новострой-М",
        "text": "Коммунарка, Бачуринская, КРОСТ. I очередь сдана; "
        "новые корпуса II очереди строятся.",
    },
    {
        "url": "https://news.example.org/2026/complaint",
        "title": "Дольщики требуют встречи",
        "text": "Покупатели ЖК «Новая Звезда» в Коммунарке направили претензию КРОСТу.",
    },
    {
        "url": "https://reviews.example.org/object/123",
        "title": "Что происходит в Новой Звезде",
        "text": "Вопросы покупателей жилья, КРОСТ, Бачуринская улица",
    },
    {
        "url": "https://t.me/s/novaya_zvezda_2",
        "title": "Telegram: View @novaya_zvezda_2",
        "text": "Чат соседей и дольщиков",
    },
    {
        "url": "https://krost.ru/projects/novaja-zvezda-2/",
        "title": "Новая Звезда II очередь",
        "text": "Официальная страница проекта",
    },
    {
        "url": "https://xn--80az8a.xn--d1aqf.xn--p1ai/servisy/object/66399",
        "title": "Карточка дома",
        "text": "Застройщик и разрешительные документы",
    },
]


@pytest.mark.parametrize("candidate", BAD)
def test_saved_collision_results_do_not_spend_a_read_slot(candidate):
    assert not report_search.candidate_relevance(KROST, candidate)["accepted"]


@pytest.mark.parametrize("candidate", GOOD)
def test_short_alias_stays_eligible_on_genuine_partner_complaint_media_and_chat(candidate):
    assert report_search.candidate_relevance(KROST, candidate)["accepted"]


def test_brand_and_location_match_flexible_cyrillic_latin_name_forms():
    dogma = {
        "name": "Догма Парк",
        "developer": "DOGMA",
        "region": "Краснодарский край, Краснодар",
        "aliases": ["DOGMA PARK", "Догма Парк"],
    }
    assert report_search.candidate_relevance(
        dogma,
        {
            "url": "https://example.org/dogmapark",
            "title": "DOGMA PARK — новости покупателей",
            "text": "Краснодар, задержки ответа покупателям",
        },
    )["accepted"]
    assert report_search.candidate_relevance(
        KROST,
        {
            "url": "https://press.example.org/story",
            "title": "Дольщики Новой Звезды ждут ответ",
            "text": "Застройщик КРОСТ",
        },
    )["accepted"]


def test_tracking_slash_and_percent_encoding_dedup_does_not_remove_functional_queries():
    base = "https://krost.ru/projects/novaja-zvezda-2"
    assert report_search.research_url(base + "/?erid=ad&utm_campaign=test#photo") == base
    assert report_search.research_url(
        "https://example.org/%D0%B4%D0%BE%D0%BC/"
    ) == report_search.research_url("https://example.org/дом")
    assert report_search.research_url(
        "https://example.org/thread?before=10&page=2"
    ) != report_search.research_url("https://example.org/thread?before=20&page=2")


def test_eighteen_entities_keep_ten_valid_grounded_rows_without_snippet_evidence():
    text = " ".join(f"событие номер {i}" for i in range(18))
    docs = [
        {
            "url": "https://example.org/read",
            "title": "ЖК",
            "text": text,
            "read_status": "loaded",
            "source_kind": "page",
        },
        {
            "url": "https://example.org/snippet",
            "title": "ЖК",
            "text": "сущность из сниппета",
            "read_status": "unavailable",
            "source_kind": "snippet",
        },
    ]
    raw = [None, {"text": "сущность из сниппета", "source_url": docs[1]["url"], "kind": "event"}]
    raw += [
        {"text": f"событие номер {i}", "source_url": docs[0]["url"], "kind": "event"}
        for i in range(18)
    ]
    result = report_search.grounded_entities({"entities": raw}, docs)
    assert len(result) == 10
    assert [entity.text for entity in result] == [f"событие номер {i}" for i in range(10)]


@pytest.fixture
def store(tmp_path, monkeypatch):
    shutil.copytree(Path("config"), tmp_path / "config")
    (tmp_path / ".env").write_text(
        "YANDEX_SEARCH_API_KEY=test\nYANDEX_FOLDER_ID=test\nOPENROUTER_API_KEY=test\n"
    )
    monkeypatch.setattr("radar.web.workflow.reserve_call", lambda *a: None)
    result = Store(tmp_path)
    with result.connect() as db:
        db.execute(
            "UPDATE records SET dataset='web',payload=? WHERE id='demo-1'",
            (json.dumps({**KROST, "urls": []}),),
        )
    return result


def test_new_search_keeps_fresh_cited_evidence_and_reads_a_new_publisher(store):
    proven = {"url": "https://msk1.ru/text/realty/news", "title": "Новая Звезда II очередь",
              "text": "КРОСТ сообщил о стройке в Коммунарке, улица Бачуринская."}
    new = {"url": "https://rbc.ru/realty/article", "title": "Новая Звезда II очередь",
           "text": "Новая Звезда, Москва, КРОСТ: подтверждён новый срок."}
    checked = datetime.now(UTC)

    def page(row):
        return Page(row["url"], row["url"], "loaded", title=row["title"], text=row["text"],
                    fetched_at=checked.isoformat())

    load_page(store, proven["url"], lambda _: page(proven))
    historical = {**proven, "read_status": "loaded", "source_kind": "page",
                  "checked_at": checked.isoformat()}
    stale = {**historical, "url": "https://msk1.ru/text/realty/old",
             "checked_at": (checked - timedelta(days=10)).isoformat()}
    other = {**historical, **BAD[4]}
    store.save_brief("demo-1", {
        "report_pipeline": {"passed": True},
        "report_research": {"documents": [historical, stale, other]},
        "report_v2": {"meta": {"schema_version": "8"}, "diagnosis": {
            "sources": [{"url": row["url"]} for row in (historical, stale, other)],
        }},
    })
    for verdict in ("hold", "watch"):
        store.save_brief("demo-1", {
            "report_v2": {"meta": {"schema_version": "8"}},
            "report_qualification": {"verdict": verdict},
            "report_pipeline": {"passed": True},
        })
    reads = []

    def reader(url):
        reads.append(url)
        assert url == new["url"]
        return page(new)

    result = report_search.ensure_research(
        store, "demo-1", "refresh", object(), searcher=lambda *a: [new],
        reader=reader, entity_finder=lambda *a: [],
    )
    assert reads == [new["url"]]
    assert [row["url"] for row in result["documents"]] == [proven["url"], new["url"]]
    assert result["documents"][0]["previously_cited"] is True


def test_preselection_leaves_budget_for_one_real_page_and_reuses_existing_search_cache(
    store, monkeypatch
):
    searches, reads = [], []
    base = GOOD[4]["url"].rstrip("/")

    def searcher(*args):
        searches.append(args[2])
        return [
            *BAD,
            GOOD[4],
            {**GOOD[4], "url": base + "/?erid=ad"},
            {**GOOD[4], "url": base + "/"},
        ]

    def reader(url):
        reads.append(url)
        return Page(
            url,
            url,
            "loaded",
            title="Новая Звезда II очередь",
            text="КРОСТ: сдача II очереди в 2027 году.",
            fetched_at=datetime.now(UTC).isoformat(),
        )

    with httpx.Client(
        transport=httpx.MockTransport(lambda r: pytest.fail("Unexpected network"))
    ) as client:
        result = report_search.ensure_research(
            store,
            "demo-1",
            "test",
            client,
            searcher=searcher,
            reader=reader,
            entity_finder=lambda *a: [],
        )
        again = report_search.ensure_research(
            store,
            "demo-1",
            "test2",
            client,
            searcher=lambda *a: pytest.fail("Repeated search"),
            reader=lambda *a: pytest.fail("Repeated read"),
            entity_finder=lambda *a: [],
        )
        monkeypatch.setattr(report_search, "RELEVANCE_VERSION", "test-new-quality-rules")
        refreshed = report_search.ensure_research(
            store,
            "demo-1",
            "quality-refresh",
            client,
            searcher=lambda *a: pytest.fail("Quality revision must reuse paid query cache"),
            reader=lambda *a: pytest.fail("Quality revision must reuse loaded page cache"),
            entity_finder=lambda *a: [],
        )
    assert reads == [base]
    assert result["page_reads"] == 1 and result["rejected_candidate_urls"] == len(BAD)
    assert len(result["documents"]) == 1 and result["documents"][0]["source_kind"] == "page"
    assert len(searches) <= 40 and result["passes"] == 2 and again["cached"]
    assert refreshed["cache_hits"] > 0 and refreshed["signature"] != result["signature"]


def test_loaded_page_must_pass_scope_again_and_cannot_borrow_its_search_snippet(store):
    def searcher(*args):
        return [
            {
                "url": "https://example.org/redirected-story",
                "title": "Дольщики Новой Звезды",
                "text": "КРОСТ, Коммунарка: ключи задержаны",
            }
        ]

    def reader(url):
        return Page(
            url,
            url,
            "loaded",
            title="ЖК «Другой проект»",
            text="Статья о другом объекте. Смотрите также: Новая Звезда II очередь, КРОСТ.",
            fetched_at=datetime.now(UTC).isoformat(),
        )

    result = report_search.ensure_research(
        store,
        "demo-1",
        "wrong-loaded-page",
        object(),
        searcher=searcher,
        reader=reader,
        entity_finder=lambda *a: [],
    )
    document = result["documents"][0]
    assert document["read_status"] == "out_of_scope" and document["text"] == ""
    assert document["source_kind"] == "snippet"
    assert "ключи задержаны" in document["search_snippet"]


def test_revised_entity_contract_refreshes_completed_error_using_existing_paid_search_cache(
    store, monkeypatch
):
    searches, reads, entity_calls = [], [], []

    def searcher(*args):
        searches.append(args[2])
        return [GOOD[4]]

    def reader(url):
        reads.append(url)
        return Page(
            url,
            url,
            "loaded",
            title="Новая Звезда II очередь",
            text="КРОСТ: сдача II очереди в 2027 году.",
            fetched_at=datetime.now(UTC).isoformat(),
        )

    def old_entity_finder(*args):
        entity_calls.append("old")
        raise ValueError("Модель не завершила ответ")

    def new_entity_finder(*args):
        entity_calls.append("new")
        return []

    monkeypatch.setattr(report_search, "ENTITY_VERSION", "old-truncated-entity-contract")
    old = report_search.ensure_research(
        store,
        "demo-1",
        "old-contract",
        object(),
        searcher=searcher,
        reader=reader,
        entity_finder=old_entity_finder,
    )
    assert old["completed"] and old["errors"] == [
        {"stage": "entities", "detail": "Модель не завершила ответ"}
    ]
    paid_calls = len(searches)
    monkeypatch.setattr(report_search, "ENTITY_VERSION", "new-complete-entity-contract")
    refreshed = report_search.ensure_research(
        store,
        "demo-1",
        "new-contract",
        object(),
        searcher=lambda *args: pytest.fail("Entity contract must reuse paid search cache"),
        reader=lambda *args: pytest.fail("Entity contract must reuse loaded page cache"),
        entity_finder=new_entity_finder,
    )
    again = report_search.ensure_research(
        store,
        "demo-1",
        "same-contract",
        object(),
        entity_finder=lambda *args: pytest.fail("Completed new contract must be cached"),
    )
    assert refreshed["completed"] and not refreshed["errors"]
    assert refreshed["signature"] != old["signature"] and refreshed["cache_hits"] > 0
    assert again["cached"] and entity_calls == ["old", "new"]
    assert len(reads) == 1 and len(searches) == paid_calls <= 40


def test_redaction_revision_rebuilds_only_from_unmodified_raw_page_cache(store, monkeypatch):
    raw = "КРОСТ: Новая Звезда II очередь. Литер21III кв.2026IV кв.2026+1 кв."
    correct_redact = report_search.redact
    searches, reads = [], []

    def searcher(*args):
        searches.append(args[2])
        return [GOOD[4]]

    def reader(url):
        reads.append(url)
        return Page(
            url,
            url,
            "loaded",
            title="Новая Звезда II очередь",
            text=raw,
            fetched_at=datetime.now(UTC).isoformat(),
        )

    monkeypatch.setattr(report_search, "REDACTION_VERSION", "old-apartment-quarter-bug")
    monkeypatch.setattr(
        report_search,
        "redact",
        lambda text, names: correct_redact(text, names).replace("кв.2026", "[квартира]"),
    )
    old = report_search.ensure_research(
        store,
        "demo-1",
        "old-redaction",
        object(),
        searcher=searcher,
        reader=reader,
        entity_finder=lambda *args: [],
    )
    assert "[квартира]" in old["documents"][0]["text"]
    paid_calls = len(searches)
    monkeypatch.setattr(report_search, "REDACTION_VERSION", "new-calendar-quarter-guard")
    monkeypatch.setattr(report_search, "redact", correct_redact)
    refreshed = report_search.ensure_research(
        store,
        "demo-1",
        "new-redaction",
        object(),
        searcher=lambda *args: pytest.fail("Redaction revision must reuse paid search cache"),
        reader=lambda *args: pytest.fail("Redaction revision must reuse raw page cache"),
        entity_finder=lambda *args: [],
    )
    assert refreshed["documents"][0]["text"] == raw
    assert refreshed["signature"] != old["signature"]
    assert refreshed["cache_hits"] > 0 and len(searches) == paid_calls and len(reads) == 1


def test_entity_call_uses_only_read_pages_and_tolerates_overfull_model_array(store, monkeypatch):
    document = {
        "url": "https://example.org/read",
        "title": "ЖК",
        "text": " ".join(f"событие номер {i}" for i in range(18)),
        "read_status": "loaded",
        "source_kind": "page",
    }
    snippet = {
        "url": "https://example.org/no-read",
        "title": "ЖК",
        "text": "do-not-send-snippet",
        "read_status": "unavailable",
        "source_kind": "snippet",
    }
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        response = {
            "entities": [
                {"text": f"событие номер {i}", "source_url": document["url"], "kind": "event"}
                for i in range(18)
            ]
        }
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(response)}}]}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = report_search.discover_entities(
            store, KROST, [document, snippet], client, "entities"
        )
        cached = report_search.discover_entities(
            store, KROST, [document, snippet], client, "cached"
        )
        assert report_search.discover_entities(store, KROST, [snippet], client, "empty") == []
        assert len(calls) == 1
        body = calls[0]
        assert body["max_tokens"] == 4000
        assert body["max_tokens"] >= body.get("reasoning", {}).get("max_tokens", 0) + 3000
        assert "Не более 10" in body["messages"][0]["content"]
        assert "до 70 символов" in body["messages"][0]["content"]
        entity_schema = report_search.Entities.model_json_schema()
        assert entity_schema["properties"]["entities"]["maxItems"] == 10
        # A revised step contract gets its own cache key, while identical calls above reused it.
        monkeypatch.setattr(report_search, "ENTITY_PROMPT", report_search.ENTITY_PROMPT + " Тест.")
        report_search.discover_entities(store, KROST, [document], client, "revised-contract")
    assert len(result) == 10 and cached == result and len(calls) == 2
    assert "do-not-send-snippet" not in json.dumps(calls)
