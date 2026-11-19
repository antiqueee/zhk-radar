import copy
import json

import pytest
import test_report_pipeline_v5 as pipeline_fixtures

from radar.reports import pipeline_v8 as pipeline
from radar.reports import research_anchors as anchors
from radar.web import report_evidence as contracts


@pytest.fixture
def materials():
    # The construction quote is the main article, with no neighbouring-story feed.
    construction = "В настоящий момент активно выполняются монолитные работы выше нулевого уровня."
    addressee = (
        "Застройщик «Эталон» перенёс срок выдачи ключей в ЖК Nagatino I-Land "
        "с марта 2026 на декабрь 2027"
    )
    facts = [
        {
            "source_n": 3,
            "text": construction,
            "date": "2026-10-28",
            "who_says": "СМИ",
            "kind": "fact",
            "verbatim": construction,
        },
        {
            "source_n": 6,
            "text": addressee,
            "date": "2026-01-30",
            "who_says": "СМИ",
            "kind": "fact",
            "verbatim": addressee,
        },
    ]
    context = {
        "as_of": "2026-11-17",
        "object": {"name": "Нагатино Ай-Лэнд", "developer": "Эталон"},
        "saved_passport": {"status": "unknown", "addressee": "none"},
        "sources": [
            {
                "n": fact["source_n"],
                "kind": "page",
                "contour": "media",
                "evidence_date": fact["date"],
                "text": fact["verbatim"],
            }
            for fact in facts
        ],
    }
    return context, facts


def test_candidates_use_the_same_gate_and_exact_fact_keys_without_assigning_status(materials):
    context, facts = materials
    before = copy.deepcopy(materials)
    result = anchors.anchored_context(context, facts)
    candidates = result["eligible_passport_candidates"]
    keys = list(anchors.fact_registry(facts))
    assert candidates == {
        "status": [{"value": "under_construction", "date": "2026-10-28", "fact_key": keys[0]}],
        "developer_status": [],
        "addressee": [{"value": "developer_active", "date": "2026-01-30", "fact_key": keys[1]}],
    }
    assert result["saved_passport"] == {"status": "unknown", "addressee": "none"}
    assert materials == before
    registry = anchors.fact_registry(facts)
    for field, rows in candidates.items():
        for item in rows:
            row = registry[item["fact_key"]]
            patch = {
                "developer_brand": "Эталон",
                "developer_legal": None,
                "tech_customer": None,
                field: item["value"],
                field + "_refs": [row["fact"]["source_n"]],
                field + "_fact_indexes": [row["index"]],
            }
            if field != "addressee":
                patch[field + "_date"] = item["date"]
            assert contracts._passport_proof(
                contracts.Passport.model_construct(**patch),
                field,
                {s["n"]: s for s in context["sources"]},
                facts,
                context["as_of"],
            ) == [row["index"]]


@pytest.mark.parametrize("invalid", ["undated", "old_event", "review", "snippet", "wrong_quote"])
def test_candidates_do_not_bypass_date_contour_or_exact_quote_rules(materials, invalid):
    context, facts = materials
    source, fact = context["sources"][0], facts[0]
    if invalid == "undated":
        fact["date"] = source["evidence_date"] = None
        source["checked_at"] = "2026-11-17T09:00:00Z"
    elif invalid == "old_event":
        fact["date"] = "2024-10-28"
        source["evidence_date"] = "2026-10-28"
    elif invalid == "review":
        source["contour"] = "reviews"
    elif invalid == "snippet":
        source["source_kind"] = "snippet"
    else:
        fact["verbatim"] += " Не было в источнике."
    assert anchors.eligible_passport_candidates(context, facts)["status"] == []


@pytest.mark.parametrize(
    "text",
    [
        "Планируется выполнение монолитных работ выше нулевого уровня.",
        "Монолитные работы выше нулевого уровня не выполняются.",
    ],
)
def test_future_and_negated_construction_are_not_eligible(materials, text):
    context, facts = materials
    facts[0]["text"] = facts[0]["verbatim"] = context["sources"][0]["text"] = text
    assert anchors.eligible_passport_candidates(context, facts)["status"] == []


def test_exact_party_check_does_not_invent_brand_aliases(materials):
    context, facts = materials
    context["object"]["developer"] = "Другой Эталон"
    assert anchors.eligible_passport_candidates(context, facts)["addressee"] == []


def test_source_publication_can_date_a_fact_but_a_checked_at_stamp_cannot(materials):
    context, facts = materials
    facts[0]["date"] = None
    rows = anchors.eligible_passport_candidates(context, facts)["status"]
    assert rows and rows[0]["date"] == "2026-10-28"
    context["sources"][0].pop("evidence_date")
    context["sources"][0]["checked_at"] = "2026-11-17T09:00:00Z"
    assert anchors.eligible_passport_candidates(context, facts)["status"] == []


def test_real_research_invoke_has_eligible_fact_keys_but_never_raw_source_bodies(
    materials, monkeypatch, tmp_path
):
    class ReachedResearch(RuntimeError):
        pass

    context, facts = materials
    monkeypatch.setattr(pipeline_fixtures, "pipeline", pipeline)
    store, payload = pipeline_fixtures.harness.__wrapped__(monkeypatch, tmp_path)
    payload.update(name=context["object"]["name"], developer=context["object"]["developer"])
    sources = copy.deepcopy(context["sources"])
    for source in sources:
        source["url"] = f"https://example.org/article-{source['n']}"
        source["text"] += "\nUNEXTRACTED_SOURCE_BODY_MUST_NOT_LEAK"
        source["messages"] = [{"text": "UNEXTRACTED_MESSAGE_MUST_NOT_LEAK"}]
    monkeypatch.setattr(pipeline, "registry_sources", lambda _payload: sources)
    calls = []

    def invoke(_client, _url, _headers, body, label):
        calls.append(label)
        data = json.loads(body["messages"][1]["content"])["data"]
        if label.startswith("Отчёт: facts:"):
            selected = {source["n"] for source in data["sources"]}
            return pipeline_fixtures.response(
                {"facts": [fact for fact in facts if fact["source_n"] in selected]}
            )
        assert label == "Отчёт: research"
        keys = list(anchors.fact_registry(facts))
        assert data["eligible_passport_candidates"]["status"] == [
            {"value": "under_construction", "date": "2026-10-28", "fact_key": keys[0]}
        ]
        assert data["eligible_passport_candidates"]["addressee"] == [
            {"value": "developer_active", "date": "2026-01-30", "fact_key": keys[1]}
        ]
        assert data["eligible_passport_candidates"]["developer_status"] == []
        assert all(not {"text", "messages"} & source.keys() for source in data["sources"])
        assert all("UNEXTRACTED_" not in message["content"] for message in body["messages"])
        assert keys == [
            fact["fact_key"] for group in data["facts_by_source"] for fact in group["facts"]
        ]
        raise ReachedResearch

    with pytest.raises(ReachedResearch):
        pipeline.generate(
            store,
            "record",
            "candidate-integration",
            client=object(),
            invoke=invoke,
            researcher=lambda *_args, **_kwargs: None,
            renderer=lambda _report: pytest.fail("No PDF authoring in a context boundary test"),
        )
    assert calls == ["Отчёт: facts:0", "Отчёт: research"] and not store.saved
