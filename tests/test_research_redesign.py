import io
import shutil
from pathlib import Path

import pytest
from pypdf import PdfReader

from radar.web import prospecting, screening
from radar.web.brief_service import payload_for
from radar.web.discovery import SearchRequest, query_plan
from radar.web.pdf import render_pdf
from radar.web.sources import research_excerpt
from radar.web.store import Store


@pytest.fixture
def store(tmp_path):
    shutil.copytree("config", tmp_path / "config")
    return Store(tmp_path)


def test_country_plan_has_regional_coverage():
    cfg, queries = query_plan(Path("."), SearchRequest(region="russia"))
    assert len(cfg["regions"]["russia"]) == 16
    assert len(queries) == 32
    assert all(any(region in q for region in cfg["regions"]["russia"]) for q in queries)
    assert any("форум" in q for q in queries)


def test_registry_portfolios_are_search_seeds_not_object_facts(store):
    def fetcher(store, slug):
        return {
            "rows": [
                {"developer": "Тест Девелопер", "delayed_area": 100, "delayed_share_pct": 10},
                {"developer": "Без переносов", "delayed_area": 0, "delayed_share_pct": 0},
            ]
        }

    result = prospecting.seeds(store, ["Москва"], fetcher)
    assert len(result["developers"]) == 2
    assert len(result["queries"]) == 1
    assert "Тест Девелопер" in result["queries"][0]
    assert store.records("web") == []


def test_chat_binding_needs_location_or_corroborated_exact_link():
    record = {"name": "ЖК Особенное название", "region": "Москва", "building": None}
    metadata = {"public": True, "group": True, "title": "Особенное название — соседи"}
    assert not screening.matches(record, metadata)[0]
    assert not screening.matches(record, metadata, {"context_text": "Особенное название Москва"})[0]
    assert screening.matches(
        record,
        metadata,
        {"context_text": "Особенное название Москва", "verified_url": "https://t.me/sample_chat"},
    )[0]
    assert not screening.matches(
        record,
        {**metadata, "group": False},
        {"context_text": "Особенное название Москва", "verified_url": "https://t.me/sample_chat"},
    )[0]
    assert not screening.matches(
        {**record, "building": "корпус 2"},
        metadata,
        {"context_text": "Особенное название Москва", "verified_url": "https://t.me/sample_chat"},
    )[0]


def test_excerpt_keeps_late_problem_context():
    text = "Навигация " * 1000 + "В ЖК Проверяемый перенос срока на следующий год."
    result = research_excerpt(text, 4000)
    assert len(result) <= 4000
    assert "ЖК Проверяемый перенос" in result
    assert "[пропуск текста]" in result


def test_two_page_brief_separates_facts_and_offer(store):
    payload = payload_for(store, "demo-1")
    pdf = PdfReader(io.BytesIO(render_pdf(payload)))
    assert len(pdf.pages) == 2
    assert "Что происходит" in pdf.pages[0].extract_text()
    assert "История чатов не прочитана" in pdf.pages[0].extract_text()
    assert "Что предложить девелоперу" in pdf.pages[1].extract_text()
    assert "Как измерить" in pdf.pages[1].extract_text()
    assert "МЕГАПОЛИС" in pdf.pages[0].extract_text()


def test_seed_lead_is_neutral_and_idempotent(store):
    item = {"id": "agency-1", "name": "Намёткин Тауэр", "region": "Москва", "developer": "Основа"}
    ids, added = prospecting.save_leads(store, [item])
    assert added == 1
    assert prospecting.save_leads(store, [item]) == (ids, 0)
    record = store.get(ids[0])
    assert record["verification"] == "research_lead"
    assert record["urls"] == []
    assert record["assessment"] is None
    assert not record["evidence"]


def test_targeted_plan_has_four_specific_queries():
    _, queries = query_plan(Path("."), SearchRequest(region="moscow", target="Намёткин Тауэр"))
    assert len(queries) == 4
    assert all("Намёткин Тауэр" in q for q in queries)


def test_erz_does_not_accept_russia_values_for_moscow(store):
    from radar.web.registry import fetch_erz

    html = (
        '<app-developer-item><div class="developer-td-3">'
        '<a href="/zastroyschiki/test">Test</a></div></app-developer-item>'
    )

    def transport(url):
        assert "regionKey=143443001" in url
        return 200, {}, html.encode()

    with pytest.raises(ValueError, match="не подтвердил регион"):
        fetch_erz(store, "moskva", transport)


def test_chat_binding_reads_exact_anchor_on_project_page(store):
    from radar.web import chat_workflow
    from radar.web.sources import Page

    record = {"name": "ЖК Наметкин Тауэр", "region": "Москва", "building": None}
    url = "https://t.me/nametkin_tower_zhk"
    rows = [
        {"url": "https://example.org/nametkin", "title": "ЖК Наметкин Тауэр", "text": "Чат соседей"}
    ]

    def searcher(*args):
        return rows

    def reader(address):
        return Page(
            address,
            address,
            "loaded",
            title="ЖК Наметкин Тауэр",
            text="Наметкин Тауэр, Москва",
            links=[url],
        )

    result = chat_workflow.corroborate(
        store, record, {"url": url}, "binding", searcher=searcher, reader=reader
    )
    assert result["verified_url"] == url
    assert result["source_url"] == rows[0]["url"]


def test_project_title_binds_literal_quote_without_invented_name():
    from radar.web.research_answers import Answer, Answers, validate_answers

    record = {"name": "ЖК Наметкин Тауэр"}
    doc = {
        "title": "Намёткин Тауэр — сроки",
        "text": "Передача помещений\nзапланирована на апрель.",
        "url": "https://example.org/project",
        "source_id": "test",
        "source_kind": "page",
    }
    answer = Answer(
        question="deadlines", source_index=0, quote="Передача помещений запланирована на апрель."
    )
    assert len(validate_answers(Answers(answers=[answer]), record, [doc])) == 1
    with pytest.raises(ValueError):
        validate_answers(
            Answers(
                answers=[answer.model_copy(update={"quote": answer.quote + " Наметкин Тауэр"})]
            ),
            record,
            [doc],
        )


def test_quarters_are_not_apartment_contacts():
    from radar.web.chats import redact

    for value in ["II кв. 2026", "IV кв 2027", "2 кв. 2026", "III кв. 2025 года"]:
        assert redact(value, []) == value
    for value in ["кв. 99", "квартира 2026", "II кв. №2026", "кв. 2026"]:
        assert "[квартира]" in redact(value, [])
