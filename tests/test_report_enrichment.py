"""Offline enrichment integration: stable proof identities and paid-work reuse."""

import json
from copy import deepcopy

import pytest

from radar.reports import pipeline_v8 as pipeline
from radar.web import report_search, workflow
from radar.web.store import Store


class EmptyStore(Store):
    def seed(self):
        """An isolated real SQLite workspace contains no demo/user records."""


def document(ident, url, text, *, kind="page", checked="2026-11-17T08:00:00+00:00"):
    row = {
        "source_id": ident,
        "url": url,
        "title": "ЖК Тест: материалы",
        "text": text,
        "source_kind": kind,
        "read_status": "loaded",
        "published_at": "2026-11-01" if kind == "page" else None,
        "checked_at": checked,
        "contour": "media" if kind == "page" else "communities",
    }
    if kind == "chat":
        row["messages"] = [{"url": url + "/10", "date": "2026-11-02T09:00:00+00:00", "text": text}]
    return row


@pytest.fixture
def docs():
    return [
        document(
            "original-page",
            "https://news.example/original",
            "Покупатели сообщили о переносе срока.",
        ),
        document(
            "report-chat:public-group",
            "https://t.me/public_group",
            "Я прошу назвать новый срок передачи.",
            kind="chat",
        ),
        document(
            "followup-page",
            "https://news.example/company-response",
            "Компания объяснила перенос изменением проекта.",
        ),
    ]


@pytest.fixture
def payload(docs):
    return {
        "dataset": "imported",
        "name": "ЖК Тест",
        "developer": "Застройщик Тест",
        "region": "Москва",
        "report": {},
        "topics": [],
        "urls": [],
        "report_research": {
            "version": "research-test",
            "signature": "base-signature",
            "documents": deepcopy(docs[:2]),
            "search_calls": 29,
            "page_reads": 25,
            "cache_hits": 0,
        },
    }


@pytest.fixture
def store(tmp_path):
    result = EmptyStore(tmp_path)
    with result.connect() as db:
        db.execute(
            "INSERT INTO records(id,dataset,payload,updated_at) VALUES(?,?,?,?)",
            ("object", "imported", json.dumps({"name": "ЖК Тест"}), "2026-11-17"),
        )
    return result


def persist_numbers(payload, sources):
    payload["report_research"]["source_registry_numbers"] = {
        source.get("url")
        or str(source.get("source_id") or source.get("id") or source["n"]): source["n"]
        for source in sources
    }


def keyed(sources):
    return {
        source.get("url") or source.get("source_id") or source.get("id"): source["n"]
        for source in sources
    }


def test_source_numbers_survive_new_document_inserted_before_existing_chat(payload, docs):
    before = pipeline.registry_sources(payload)
    original_numbers = keyed(before)
    persist_numbers(payload, before)
    payload["report_research"]["documents"].insert(1, deepcopy(docs[2]))
    after = pipeline.registry_sources(payload)
    assert keyed(after)[docs[0]["url"]] == original_numbers[docs[0]["url"]]
    assert keyed(after)[docs[1]["url"]] == original_numbers[docs[1]["url"]]
    assert keyed(after)[docs[2]["url"]] > max(original_numbers.values())
    assert len({source["n"] for source in after}) == len(after) == 3


def test_url_less_chat_identity_is_stable_when_documents_are_inserted(payload, docs):
    payload["report_research"]["documents"] = [deepcopy(docs[0])]
    payload["chat_stats"] = {
        "report_sources": [
            {
                "id": "uploaded-chat:stable",
                "url": None,
                "text": "Я жду ответа по срокам передачи.",
                "kind": "chat",
                "topics": [],
            }
        ]
    }
    first = pipeline.registry_sources(payload)
    old_chat = next(s for s in first if not s.get("url"))
    persist_numbers(payload, first)
    payload["report_research"]["documents"].append(deepcopy(docs[2]))
    second = pipeline.registry_sources(payload)
    new_chat = next(s for s in second if not s.get("url"))
    assert new_chat["n"] == old_chat["n"]
    assert (new_chat.get("source_id") or new_chat.get("id")) == "uploaded-chat:stable"


def test_merge_followup_deduplicates_url_and_counts_each_signed_round_once(store, payload, docs):
    before = pipeline.registry_sources(payload)
    persist_numbers(payload, before)
    result = {
        "signature": "one-gap-round",
        "search_calls": 3,
        "page_reads": 2,
        "cache_hits": 1,
        "documents": [
            {
                **docs[0],
                "source_id": "duplicate-fetch",
                "url": docs[0]["url"] + "?utm_source=retry#section",
            },
            deepcopy(docs[2]),
        ],
        "cached": False,
        "coverage": {"gaps": ["cause"]},
    }
    first = pipeline._merge_followup(store, "object", payload, result)
    assert [d["source_id"] for d in first] == [docs[2]["source_id"]]
    second = pipeline._merge_followup(store, "object", payload, {**result, "cached": True})
    assert second == []
    research = payload["report_research"]
    assert len(research["documents"]) == 3
    assert [research[k] for k in ("search_calls", "page_reads", "cache_hits")] == [32, 27, 1]
    assert research["followup_rounds"] == ["one-gap-round"]
    assert "cached" not in research["followup"]
    persisted = report_search.get_research(store, "object")
    assert {d["source_id"] for d in persisted["documents"]} == {
        docs[0]["source_id"],
        docs[2]["source_id"],
    }
    assert persisted["source_registry_numbers"] == research["source_registry_numbers"]
    # SDK chat material stays in this report's context, not in search-page storage.
    assert docs[1]["source_id"] in {d["source_id"] for d in research["documents"]}
    assert docs[1]["source_id"] not in {d["source_id"] for d in persisted["documents"]}


def test_readable_followup_replaces_an_unavailable_document_once(store, payload, docs):
    unavailable = {**docs[2], "source_id": "attempt", "text": "", "read_status": "unavailable",
                   "source_kind": "snippet"}
    payload["report_research"]["documents"].insert(1, unavailable)
    original_numbers = keyed(pipeline.registry_sources(payload))
    persist_numbers(payload, pipeline.registry_sources(payload))
    result = {"signature": "original-link", "page_reads": 1, "documents": [docs[2]]}

    assert pipeline._merge_followup(store, "object", payload, result) == [docs[2]]
    assert pipeline._merge_followup(store, "object", payload, result) == []
    sources = keyed(pipeline.registry_sources(payload))
    assert {key: sources[key] for key in original_numbers} == original_numbers
    assert sources[docs[2]["url"]] > max(original_numbers.values())
    persisted = report_search.get_research(store, "object")
    assert [d["source_id"] for d in persisted["documents"]] == [
        docs[0]["source_id"], docs[2]["source_id"],
    ]
    assert persisted["page_reads"] == 26


def fingerprint(payload):
    return pipeline._fingerprint(
        payload,
        pipeline.registry_sources(payload),
        [],
        [],
        {"extract": {"model": "test"}},
        "2026-11-17",
    )


def test_cached_chat_read_timestamps_and_round_counters_do_not_change_fingerprint(payload):
    before = fingerprint(payload)
    later = deepcopy(payload)
    later["report_research"].update(created_at="later", cache_hits=20, page_reads=26)
    for doc in later["report_research"]["documents"]:
        doc["checked_at"] = "2026-11-17T08:10:00+00:00"
        doc["fetched_at"] = "2026-11-17T08:10:00+00:00"
        doc["cached"] = True
    later["report_chat_read"] = {
        "checked_at": "2026-11-17T08:10:00+00:00",
        "cache_hits": 1,
        "messages_read": 1,
    }
    assert fingerprint(later) == before


@pytest.mark.parametrize("change", ["body", "message_date", "publication_date"])
def test_fingerprint_still_changes_for_actual_content_or_its_evidence_date(payload, change):
    before = fingerprint(payload)
    after = deepcopy(payload)
    docs = after["report_research"]["documents"]
    if change == "body":
        docs[1]["text"] += " Пришёл ответ компании."
        docs[1]["messages"][0]["text"] = docs[1]["text"]
    elif change == "message_date":
        docs[1]["messages"][0]["date"] = "2026-11-03T09:00:00+00:00"
    else:
        docs[0]["published_at"] = "2026-11-03"
    assert fingerprint(after) != before


def test_fingerprint_does_not_depend_on_document_order_with_saved_source_ids(payload, docs):
    initial = pipeline.registry_sources(payload)
    persist_numbers(payload, initial)
    payload["report_research"]["documents"].append(deepcopy(docs[2]))
    extended = pipeline.registry_sources(payload)
    persist_numbers(payload, extended)
    before = fingerprint(payload)
    later = deepcopy(payload)
    later["report_research"]["documents"] = [
        later["report_research"]["documents"][i] for i in (0, 2, 1)
    ]
    assert fingerprint(later) == before


def hold_research():
    return {
        "passport": {
            "official_name": "ЖК Тест",
            "aliases": [],
            "developer_brand": None,
            "developer_legal": None,
            "developer_fact_keys": [],
            "tech_customer": None,
            "address": None,
            "official_card_url": None,
            "phases": [],
            "status": "unknown",
            "status_date": None,
            "status_fact_keys": [],
            "developer_status": "unknown",
            "developer_status_date": None,
            "developer_status_fact_keys": [],
            "addressee": "none",
            "addressee_fact_keys": [],
            "communities": [],
        },
        "evidence": [],
        "problems": [],
        "stage_signs": [],
        "manual_checks": [],
    }


def response(value, tool=False):
    message = {"content": json.dumps(value, ensure_ascii=False)}
    if tool:
        message = {
            "content": None,
            "tool_calls": [
                {
                    "type": "function",
                    "function": {
                        "name": "return_report_data",
                        "arguments": json.dumps(value, ensure_ascii=False),
                    },
                }
            ],
        }
    return {"choices": [{"finish_reason": "tool_calls" if tool else "stop", "message": message}]}


def test_native_enrichment_extracts_only_new_sources_and_repeat_reuses_saved_work(
    store, payload, docs, monkeypatch
):
    """Real generate/SQLite/cache/merge, injected tiny extractor, truthful hold gate."""
    models = {role: {"model": role} for role in ("extract", "author", "review")}
    monkeypatch.setattr(pipeline, "configuration", lambda root: {"OPENROUTER_API_KEY": "test"})
    monkeypatch.setattr(pipeline, "model_config", lambda root: models)
    monkeypatch.setattr(pipeline, "model_options", lambda cfg: cfg)
    monkeypatch.setattr(pipeline, "services", lambda root: ([], []))
    monkeypatch.setattr(pipeline, "report_progress", lambda *args: None)
    monkeypatch.setattr(workflow, "reserve_call", lambda *args: None)
    paid_calls, followup_calls = [], []

    def read_payload(*args):
        value = deepcopy(payload)
        persisted = report_search.get_research(store, "object")
        if persisted:
            value["report_research"] = persisted
            # A subsequent SDK read inserts the same chat after persisted web docs.
            value["report_research"]["documents"].append(deepcopy(docs[1]))
        return value

    monkeypatch.setattr(pipeline, "payload_for", read_payload)

    def invoke(_client, _url, _headers, body, label):
        stage = label.removeprefix("Отчёт: ")
        data = json.loads(body["messages"][1]["content"])["data"]
        paid_calls.append((stage, data))
        if stage.startswith("facts"):
            value = {
                "facts": [
                    {
                        "text": source["text"],
                        "date": source.get("published_at"),
                        "source_n": source["n"],
                        "who_says": "Источник",
                        "kind": "claim",
                        "verbatim": source["text"],
                    }
                    for source in data["sources"]
                ]
            }
        elif stage == "research":
            value = hold_research()
        else:
            pytest.fail("A truthful hold must not call authoring/critic/render: " + stage)
        return response(value, tool="tools" in body)

    def followup(*args, **kwargs):
        followup_calls.append(kwargs)
        return {
            "signature": "same-gap-round",
            "documents": [deepcopy(docs[2])],
            "search_calls": 1,
            "page_reads": 1,
            "cache_hits": 0,
            "coverage": {},
        }

    first = pipeline.generate(
        store,
        "object",
        "run1",
        client=object(),
        invoke=invoke,
        researcher=lambda *args, **kwargs: None,
        followup_researcher=followup,
        renderer=lambda *args: pytest.fail("Hold cannot render a PDF"),
    )
    assert first["payload"]["report_pipeline"]["status"] == "hold"
    extraction = [
        (stage, data["sources"]) for stage, data in paid_calls if stage.startswith("facts")
    ]
    assert [stage for stage, _ in extraction] == ["facts:0", "facts:followup:0"]
    assert {s["url"] for s in extraction[0][1]} == {d["url"] for d in docs[:2]}
    assert [s["url"] for s in extraction[1][1]] == [docs[2]["url"]]
    assert extraction[1][1][0]["n"] > max(s["n"] for s in extraction[0][1])
    assert len(first["payload"]["report_pipeline"]["facts"]["facts"]) == 3
    assert first["payload"]["report_pipeline"]["repairs"] == 0
    before = len(paid_calls)
    repeated = pipeline.generate(
        store,
        "object",
        "run2",
        client=object(),
        invoke=invoke,
        researcher=lambda *args, **kwargs: None,
        followup_researcher=followup,
        renderer=lambda *args: pytest.fail("Cached hold cannot render a PDF"),
    )
    assert repeated["id"] == first["id"]
    assert len(paid_calls) == before
    assert len(followup_calls) == 1
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM briefs").fetchone()[0] == 1


def test_research_model_cache_ignores_observation_metadata_but_keeps_coverage_evidence(
    store, payload, monkeypatch
):
    """Bypass the brief cache so this exercises the actual model-step cache key."""
    models = {role: {"model": role} for role in ("extract", "author", "review")}
    monkeypatch.setattr(pipeline, "configuration", lambda root: {"OPENROUTER_API_KEY": "test"})
    monkeypatch.setattr(pipeline, "model_config", lambda root: models)
    monkeypatch.setattr(pipeline, "model_options", lambda cfg: cfg)
    monkeypatch.setattr(pipeline, "services", lambda root: ([], []))
    monkeypatch.setattr(pipeline, "report_progress", lambda *args: None)
    monkeypatch.setattr(pipeline, "_saved_fingerprint", lambda *args: None)
    monkeypatch.setattr(workflow, "reserve_call", lambda *args: None)
    current = deepcopy(payload)
    current["report_research"].update(
        completed=True,
        created_at="first-clock",
        errors=[],
        budget={"search_calls": 40, "page_reads": 25},
        coverage={"status": "awaiting_fact_check", "total_search_calls": 29},
    )
    current["chat_stats"] = {
        "messages_30d": 12,
        "period_start": "2026-10-18",
        "period_end": "2026-11-17",
        "report_chat_read": {"checked_at": "first-clock", "read": [{"cached": False}]},
    }
    monkeypatch.setattr(pipeline, "payload_for", lambda *args: deepcopy(current))
    paid_calls = []

    def invoke(_client, _url, _headers, body, label):
        stage = label.removeprefix("Отчёт: ")
        data = json.loads(body["messages"][1]["content"])["data"]
        paid_calls.append((stage, deepcopy(data)))
        if stage.startswith("facts"):
            value = {
                "facts": [
                    {
                        "text": s["text"],
                        "date": s.get("published_at"),
                        "source_n": s["n"],
                        "who_says": "Источник",
                        "kind": "claim",
                        "verbatim": s["text"],
                    }
                    for s in data["sources"]
                ]
            }
        elif stage == "research":
            value = hold_research()
        else:
            pytest.fail("A truthful hold cannot author or review: " + stage)
        return response(value, tool="tools" in body)

    def generate(run):
        return pipeline.generate(
            store,
            "object",
            run,
            client=object(),
            invoke=invoke,
            researcher=lambda *a, **k: None,
            followup_researcher=lambda *a, **k: {
                "signature": "one-round",
                "documents": [],
                "search_calls": 0,
                "page_reads": 0,
                "cache_hits": 0,
                "coverage": {},
            },
            renderer=lambda *a: pytest.fail("Hold cannot render"),
        )

    generate("first")
    first = next(data for stage, data in paid_calls if stage == "research")
    first_call_count = len(paid_calls)
    current["report_research"].update(
        created_at="later-clock",
        updated_at="later-clock",
        search_calls=35,
        page_reads=25,
        cache_hits=50,
    )
    current["report_research"]["coverage"]["total_search_calls"] = 35
    for doc in current["report_research"]["documents"]:
        doc.update(checked_at="later-clock", fetched_at="later-clock", cached=True)
    current["chat_stats"]["report_chat_read"] = {"checked_at": "later-clock", "cache_hits": 99}
    generate("same-evidence")
    assert len(paid_calls) == first_call_count
    assert first["search_coverage"]["completed"] is True
    assert first["search_coverage"]["budget"] == {"search_calls": 40, "page_reads": 25}
    assert first["chat_stats"]["messages_30d"] == 12
    assert first["chat_stats"]["period_end"] == "2026-11-17"
    assert "report_chat_read" not in first["chat_stats"]
    assert all(
        "checked_at" not in source and "fetched_at" not in source for source in first["sources"]
    )
    assert any(s.get("published_at") == "2026-11-01" for s in first["sources"])
    assert any(f["date"] is not None for batch in first["facts_by_source"] for f in batch["facts"])

    # A failed source remains semantically meaningful, including its complete evidence.
    reason = "Источник потребовал вход. " * 100
    current["report_research"].update(
        completed=False, errors=[{"stage": "search", "detail": reason}]
    )
    generate("changed-coverage")
    research_calls = [data for stage, data in paid_calls if stage == "research"]
    assert len(research_calls) == 2
    assert research_calls[-1]["search_coverage"]["completed"] is False
    assert research_calls[-1]["search_coverage"]["errors"][0]["detail"] == reason

    current["report_research"]["documents"][0]["published_at"] = "2026-11-03"
    generate("changed-publication-date")
    research_calls = [data for stage, data in paid_calls if stage == "research"]
    assert len(research_calls) == 3
    assert any(s.get("published_at") == "2026-11-03" for s in research_calls[-1]["sources"])
    assert any(
        f["date"] == "2026-11-03"
        for batch in research_calls[-1]["facts_by_source"]
        for f in batch["facts"]
    )
