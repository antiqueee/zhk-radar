import base64
import json
import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from radar.web import report_followup as followup
from radar.web import report_search
from radar.web.sources import Page, initialize_sources, load_page
from radar.web.store import Store

TODAY = date(2026, 11, 17)
RECORD = {
    "name": "ЖК Тестовый II очередь",
    "developer": "Строитель",
    "region": "Москва",
    "official_domains": ["builder.example"],
    "aliases": ["Тестовый 2"],
}


def materials(texts, *, kind="page", contour="official", dates=None):
    text = "\n".join(texts)
    source = {
        "n": 1,
        "url": "https://builder.example/test",
        "text": text,
        "kind": kind,
        "contour": contour,
        "published_at": "2026-11-15",
        "title": RECORD["name"],
    }
    facts = [
        {
            "source_n": 1,
            "text": text,
            "verbatim": text,
            "who_says": "Строитель",
            "date": dates[i] if dates else "2026-11-15",
        }
        for i, text in enumerate(texts)
    ]
    return [source], {"facts": facts}


def test_gap_plan_requests_missing_explanations_only_from_bound_facts():
    sources, facts = materials(["Срок передачи квартир перенесён."], contour="reviews")
    facts["facts"][0]["who_says"] = "дольщик"
    facts["facts"].append({"source_n": 99, "text": "Из-за геологии", "verbatim": "Из-за геологии"})
    plan = followup.plan_gaps(RECORD, sources, facts, today=TODAY)
    assert plan["gaps"] == ["cause", "company_position", "current_status", "phase_deadlines"]
    assert plan["grounded_facts"] == 1
    assert len(plan["queries"]) == 4
    assert all(
        any(alias in q["query"] for alias in [RECORD["name"], "Тестовый 2", "Тестовый II очередь"])
        for q in plan["queries"]
    )
    assert all("банкрот" not in q["query"] for q in plan["queries"])
    assert "site:builder.example" in plan["queries"][1]["query"]
    assert "date:>=" in plan["queries"][2]["query"]


def test_complete_recent_corpus_needs_no_boilerplate_refill():
    sources, facts = materials(
        [
            "Передача квартир перенесена из-за смены подрядчика.",
            "Компания сообщила: мы ведём строительство, готовность достигла 70%.",
        ]
    )
    qualification = {
        "passport": {"phases": [{"completion_date": "2027-Q2", "keys_date": "2027-12"}]}
    }
    plan = followup.plan_gaps(RECORD, sources, facts, qualification, today=TODAY)
    assert plan["gaps"] == [] and plan["queries"] == []


@pytest.mark.parametrize(
    "dates,kind", [(["2024-11-15"], "page"), (["2027-01-01"], "page"), (["2026-11-15"], "snippet")]
)
def test_old_future_and_snippet_status_do_not_close_current_status_gap(dates, kind):
    sources, facts = materials(["Компания ведёт строительство."], dates=dates, kind=kind)
    plan = followup.plan_gaps(RECORD, sources, facts, today=TODAY)
    assert "current_status" in plan["gaps"]


def test_unknown_cause_and_no_comment_are_not_company_position():
    sources, facts = materials(
        ["Срок перенесён. Причина переноса неизвестна. Компания не прокомментировала ситуацию."]
    )
    plan = followup.plan_gaps(RECORD, sources, facts, today=TODAY)
    assert {"cause", "company_position"} <= set(plan["gaps"])


def test_request_to_explain_a_delay_is_not_an_explanation():
    sources, facts = materials(
        ["Дольщики просят объяснить причины переноса срока передачи квартир."]
    )
    plan = followup.plan_gaps(RECORD, sources, facts, today=TODAY)
    assert "cause" in plan["gaps"]


def test_public_sdk_candidates_are_hints_without_invites_bots_or_false_read_claim():
    sources = [
        {
            "url": "https://builder.example/test",
            "links": [
                "https://t.me/test_chat",
                "https://t.me/test_chat/42",
                "https://t.me/+private",
                "https://t.me/joinchat/private",
                "https://t.me/testbot",
                "https://t.me/share?x=1",
            ],
        }
    ]
    rows = followup.public_candidates(RECORD, sources)
    assert len(rows) == 1 and rows[0]["url"] == "https://t.me/test_chat"
    assert rows[0]["requires_inspection"] is True
    assert "messages" not in rows[0] and "read_status" not in rows[0]


@pytest.fixture
def store(tmp_path, monkeypatch):
    shutil.copytree(Path("config"), tmp_path / "config")
    (tmp_path / ".env").write_text("YANDEX_SEARCH_API_KEY=test\nYANDEX_FOLDER_ID=test\n")
    monkeypatch.setattr("radar.web.workflow.reserve_call", lambda *args: None)
    result = Store(tmp_path)
    with result.connect() as db:
        db.execute(
            "UPDATE records SET dataset='web',payload=? WHERE id='demo-1'", (json.dumps(RECORD),)
        )
    report_search.initialize(result)
    return result


def base_research(store, *, searches=36, reads=21, documents=None):
    value = {"search_calls": searches, "page_reads": reads, "documents": documents or []}
    with store.connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO report_research VALUES(?,?,?)",
            ("demo-1", json.dumps(value), datetime.now(UTC).isoformat()),
        )


def test_followup_is_bounded_cached_and_keeps_unavailable_sources_honest(store):
    base_research(store)
    sources, facts = materials(["Срок передачи квартир перенесён."])
    searches, reads = [], []

    def searcher(client, cfg, query, period):
        searches.append(query)
        return [
            {
                "url": f"https://builder.example/news/{i}",
                "title": RECORD["name"],
                "text": "Москва, Строитель",
            }
            for i in range(8)
        ]

    def reader(url):
        reads.append(url)
        status = "unavailable" if url.endswith("/1") else "loaded"
        return Page(
            url,
            url,
            status,
            title=RECORD["name"],
            text="ЖК Тестовый II очередь, Москва. Строитель ведёт работы."
            if status == "loaded"
            else "",
            fetched_at=datetime.now(UTC).isoformat(),
        )

    kwargs = dict(
        sources=sources, facts=facts, client=object(), today=TODAY, searcher=searcher, reader=reader
    )
    first = followup.ensure_followup(store, "demo-1", "first", **kwargs)
    again = followup.ensure_followup(store, "demo-1", "again", **kwargs)
    assert again["cached"] is True
    assert len(searches) <= 4 and len(reads) == 4
    assert first["coverage"]["total_search_calls"] <= 40
    assert first["coverage"]["total_page_reads"] == 25
    assert first["coverage"]["gaps_resolved"] is False
    unavailable = next(d for d in first["documents"] if d["read_status"] == "unavailable")
    assert unavailable["text"] == "" and unavailable["source_kind"] == "snippet"
    assert first["coverage"]["unavailable"]
    assert all(d["contour"] == "official" for d in first["documents"])


def test_old_full_page_budget_never_forces_new_paid_requests(store):
    base_research(
        store,
        searches=40,
        reads=25,
        documents=[
            {
                "url": "https://builder.example/news",
                "read_status": "loaded",
                "links": ["https://t.me/test_public_chat"],
            }
        ],
    )
    sources, facts = materials(["Срок передачи квартир перенесён."])
    result = followup.ensure_followup(
        store,
        "demo-1",
        "full",
        sources=sources,
        facts=facts,
        client=object(),
        today=TODAY,
        searcher=lambda *args: pytest.fail("No budget"),
        reader=lambda *args: pytest.fail("No budget"),
    )
    assert result["coverage"]["status"] == "budget_exhausted"
    assert result["search_calls"] == result["page_reads"] == 0
    assert result["public_candidates"][0]["url"] == "https://t.me/test_public_chat"


def _forum_reference(url):
    return "https://pronovostroy.ru/go.php?url=" + base64.b64encode(url.encode()).decode()


def _forum_materials():
    sources, facts = materials(
        ["Собственники ЖК Тестовый II очередь обсуждали жалобы. На форуме сослались на readovka."],
        contour="reviews",
    )
    sources[0]["url"] = "https://pronovostroy.ru/topic/test"
    return sources, facts


def test_linked_source_uses_existing_read_reserve_even_after_search_credit(store):
    original = "https://readovka.news/news/237224"
    sources, facts = _forum_materials()
    base_research(
        store,
        searches=40,
        reads=24,
        documents=[
            {
                **sources[0],
                "read_status": "loaded",
                "links": [
                    _forum_reference("https://127.0.0.1/news/private"),
                    _forum_reference("https://readovka.news/"),
                    "https://fontanka.ru/2026/11/18/888888",
                    _forum_reference(original + "/?utm_source=forum"),
                    original,
                ],
            },
            {"url": original, "read_status": "unavailable", "source_kind": "snippet", "text": ""},
        ],
    )
    reads = []

    def reader(url):
        reads.append(url)
        return Page(
            url, url, "loaded", title=RECORD["name"],
            text="Дольщики ЖК Тестовый II очередь, Москва, сообщили о подаче обращения.",
            published_at="2026-11-15", fetched_at=datetime.now(UTC).isoformat(),
        )

    options = dict(
        sources=sources, facts=facts, today=TODAY,
        searcher=lambda *args: pytest.fail("No paid search credit"), reader=reader,
    )
    result = followup.ensure_followup(store, "demo-1", "linked", **options)
    cached = followup.ensure_followup(store, "demo-1", "linked-retry", **options)
    assert reads == [original]
    assert result["search_calls"] == 0 and result["coverage"]["total_page_reads"] == 25
    [document] = result["documents"]
    assert document["url"] == original and document["linked_from"] == sources[0]["url"]
    assert document["read_status"] == "loaded" and document["contour"] == "media"
    assert cached["cached"] is True


@pytest.mark.parametrize("redirect", [None, "https://unrelated.example/news/999"])
def test_linked_source_requires_matching_loaded_page_and_publisher(store, redirect):
    sources, facts = _forum_materials()
    original = "https://readovka.news/news/237224"
    base_research(store, searches=40, reads=24, documents=[{
        **sources[0], "read_status": "loaded", "links": [_forum_reference(original)],
    }])
    result = followup.ensure_followup(
        store, "demo-1", "unrelated-link", sources=sources, facts=facts, today=TODAY,
        searcher=lambda *args: pytest.fail("No paid search credit"),
        reader=lambda url: Page(
            url, redirect or url, "loaded",
            title=RECORD["name"] if redirect else "ЖК «Чужой Дом»",
            text="Тестовый II очередь, Москва" if redirect else "Обзор другого ЖК, Петербург.",
        ),
    )
    [document] = result["documents"]
    assert document["read_status"] == "out_of_scope"
    assert document["text"] == "" and document["source_kind"] == "snippet"


def test_followup_reuses_shared_search_and_seven_day_raw_page_cache(store):
    base_research(store)
    sources, facts = materials(["Срок передачи квартир перенесён."])
    plan = followup.plan_gaps(store.get("demo-1"), sources, facts, today=TODAY)
    bucket = (TODAY - followup.timedelta(days=TODAY.toordinal() % 7)).isoformat()
    url = "https://builder.example/news/same"
    row = {"url": url, "title": RECORD["name"], "text": "Москва Строитель"}
    for query in plan["queries"]:
        followup.cached(
            store,
            [report_search.VERSION, "search", bucket, query["query"], "all"],
            lambda: [row],
            "seed",
            "search",
        )
    initialize_sources(store)
    load_page(
        store,
        url,
        lambda url: Page(
            url,
            url,
            "loaded",
            title=RECORD["name"],
            text="ЖК Тестовый II очередь. Строитель, Москва. Полный текст.",
            fetched_at=datetime.now(UTC).isoformat(),
        ),
    )
    result = followup.ensure_followup(
        store,
        "demo-1",
        "cached",
        sources=sources,
        facts=facts,
        client=object(),
        today=TODAY,
        searcher=lambda *args: pytest.fail("Repeated paid query"),
        reader=lambda *args: pytest.fail("Repeated page read"),
    )
    assert len(result["documents"]) == 1 and result["cache_hits"] >= len(plan["queries"]) + 1


def test_unavailable_search_is_partial_not_absence_of_problems_and_never_leaks_errors(store):
    base_research(store)
    sources, facts = materials(["Срок передачи квартир перенесён."])

    def searcher(*args):
        raise ValueError("Credentials SECRET and raw request must not be printed")

    result = followup.ensure_followup(
        store,
        "demo-1",
        "partial",
        sources=sources,
        facts=facts,
        client=object(),
        today=TODAY,
        searcher=searcher,
        reader=lambda *args: pytest.fail("No pages"),
    )
    assert result["coverage"]["status"] == "partial"
    assert result["coverage"]["gaps_resolved"] is False
    assert result["documents"] == [] and len(result["errors"]) >= 1
    assert "SECRET" not in json.dumps(result)
