"""Offline report integration: real contracts, qualification, gating and bounded repairs."""

import copy
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pytest

from radar.reports import pipeline_v5 as pipeline
from radar.reports.schema_v5 import Facts, ReportV5
from radar.web.report_evidence import qualify

ROOT = Path(__file__).resolve().parents[1]


class MemoryStore:
    def __init__(self, root):
        self.root, self.saved = root, []
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE briefs (id INTEGER, record_id TEXT, payload TEXT)")

    @contextmanager
    def connect(self):
        yield self.db

    def save_brief(self, ident, payload):
        saved = {"id": len(self.saved) + 1, "payload": payload}
        self.saved.append(saved)
        self.db.execute(
            "INSERT INTO briefs VALUES (?,?,?)", (saved["id"], ident, json.dumps(payload))
        )
        return saved


@pytest.fixture
def materials():
    value = json.loads((ROOT / "tests/fixtures/report-research-v5.json").read_text())
    value["research"]["passport"]["phases"][0]["completion_date"] = None
    return value


@pytest.fixture
def harness(monkeypatch, tmp_path):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 16, tzinfo=tz)

    store = MemoryStore(tmp_path)
    payload = {
        "dataset": "imported",
        "name": "ЖК Тест",
        "developer": "Застройщик Тест",
        "region": "Москва",
        "report": {},
        "cases": [],
    }
    models = {role: {"model": role} for role in ("extract", "author", "review")}
    cache = {}

    def cached(_store, key, operation, *_args):
        key = json.dumps(key, sort_keys=True)
        hit = key in cache
        if not hit:
            cache[key] = operation()
        return cache[key], hit

    monkeypatch.setattr(pipeline, "configuration", lambda root: {"OPENROUTER_API_KEY": "test"})
    monkeypatch.setattr(pipeline, "model_config", lambda root: models)
    monkeypatch.setattr(pipeline, "model_options", lambda model: model)
    monkeypatch.setattr(pipeline, "payload_for", lambda *_args: copy.deepcopy(payload))
    monkeypatch.setattr(pipeline, "services", lambda root: ([], []))
    monkeypatch.setattr(pipeline, "cached", cached)
    monkeypatch.setattr(pipeline, "report_progress", lambda *_args: None)
    monkeypatch.setattr(pipeline, "datetime", Clock)
    return store, payload


def response(value, *, tool=False):
    if tool:
        return {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "return_report_data",
                                    "arguments": json.dumps(value),
                                },
                            }
                        ],
                    },
                }
            ]
        }
    return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}]}


@pytest.mark.parametrize("verdict", ["hold", "watch"])
def test_qualification_stops_before_author_and_saves_no_pdf(
    harness, materials, monkeypatch, verdict
):
    store, _payload = harness
    research = copy.deepcopy(materials["research"])
    if verdict == "hold":
        research["passport"]["developer_brand"] = None
        research["evidence"][0]["fact_indexes"] = [999]
    else:
        research["evidence"] = research["evidence"][:1]
        for problem in research["problems"]:
            problem["evidence_ids"] = [i for i in problem["evidence_ids"] if i == "e1"]
    monkeypatch.setattr(pipeline, "registry_sources", lambda payload: materials["sources"])
    queue = [{"facts": materials["facts"]}, research]
    calls = []

    def invoke(*args):
        calls.append(args[-1])
        return response(queue.pop(0), tool="tools" in args[-2])

    result = pipeline.generate(
        store, "record", "job", client=object(), invoke=invoke, researcher=lambda *a, **kw: None
    )
    assert result["payload"]["report_qualification"]["verdict"] == verdict
    assert result["payload"]["report_pipeline"]["status"] == verdict
    assert len(calls) == 2 and queue == []
    assert "report_v2" not in result["payload"]
    assert not (store.root / "data/local/report-pdfs").exists()
    if verdict == "hold":
        assert result["payload"]["report_pipeline"]["research_issues"]
        assert result["payload"]["report_qualification"]["research_issues"]
        assert result["payload"]["report_pipeline"]["repairs"] == 0
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM brief_reviews").fetchone()[0] == 1
    if verdict == "watch":
        repeated = pipeline.generate(
            store, "record", "job", client=object(), invoke=invoke, researcher=lambda *a, **kw: None
        )
        assert repeated["id"] == result["id"] and len(calls) == 2
        _payload["note"] = "Оператор уточнил корпус и адресата"
        revised = pipeline.generate(
            store, "record", "job", client=object(), invoke=invoke, researcher=lambda *a, **kw: None
        )
        assert revised["id"] != result["id"] and len(calls) == 2


def test_wide_facts_and_pdf_reference_mapping_are_separate(materials):
    materials["facts"][0]["source_n"] = 20
    wide = pipeline.ResearchFacts(facts=materials["facts"])
    assert wide.facts[0].source_n == 20
    with pytest.raises(ValueError):
        Facts.model_validate(wide.model_dump())


def full_inputs(materials):
    """Use the gold only in this offline renderer fixture, never as live evidence."""
    gold = json.loads((ROOT / "src/radar/reports/gold/nametkin-tower.json").read_text())
    text = json.dumps({k: gold[k] for k in ("situation", "diagnosis")}, ensure_ascii=False)
    status_text = "Застройщик ГК Основа ведёт строительство Намёткин Тауэр."
    stage_text = "Дольщики обсуждают подготовку коллективного обращения."
    sources, facts = [], []
    for item in gold["diagnosis"]["sources"]:
        n = item["n"]
        source = {**item, "published_at": f"2026-11-0{n}", "text": text}
        if n == 5:
            source["text"] += "\n" + status_text
        if n == 2:
            source["text"] += "\n" + stage_text
        sources.append(source)
        for index in range(0, len(text), 850):
            part = text[index : index + 850]
            facts.append(
                {
                    "text": "Материалы об объекте",
                    "date": source["published_at"],
                    "source_n": n,
                    "who_says": "источник",
                    "kind": "claim",
                    "verbatim": part,
                }
            )
        for event in gold["situation"]["timeline"]:
            if n in event["refs"]:
                facts.append(
                    {
                        "text": event["text"],
                        "date": event["date_iso"],
                        "source_n": n,
                        "who_says": "источник",
                        "kind": "claim",
                        "verbatim": event["text"],
                    }
                )
        if n == 5:
            facts.append(
                {
                    "text": status_text,
                    "date": source["published_at"],
                    "source_n": 5,
                    "who_says": "застройщик",
                    "kind": "fact",
                    "verbatim": status_text,
                }
            )
        if n == 2:
            facts.append(
                {
                    "text": stage_text,
                    "date": source["published_at"],
                    "source_n": n,
                    "who_says": "дольщики",
                    "kind": "claim",
                    "verbatim": stage_text,
                }
            )
    research = copy.deepcopy(materials["research"])
    passport = research["passport"]
    passport.update(
        official_name=gold["object"]["name"],
        developer_brand=gold["object"]["developer"],
        developer_refs=[5],
        status_refs=[5],
        addressee_refs=[5],
        developer_status_refs=[5],
        status_date="2026-11-05",
        developer_status_date="2026-11-05",
    )
    passport["phases"][0]["refs"] = [5]
    for ev, n in zip(research["evidence"], [5, 4, 3, 2], strict=True):
        ev.update(
            source_n=n,
            date_iso=f"2026-11-0{n}",
            fact_indexes=[next(i for i, f in enumerate(facts) if f["source_n"] == n)],
        )
    research["stage_signs"][0].update(
        source_n=2,
        date_iso="2026-11-02",
        fact_indexes=[
            next(i for i, f in enumerate(facts) if f["source_n"] == 2 and f["text"] == stage_text)
        ],
    )
    return gold, sources, facts, research


def execute_full(harness, materials, monkeypatch, *, critic_pass=True, overflow=False):
    store, payload = harness
    gold, sources, facts, research = full_inputs(materials)
    assert qualify(research, sources, facts, as_of="2026-11-16")["verdict"] == "report_full"
    payload.update(name=gold["object"]["name"], developer=gold["object"]["developer"])
    monkeypatch.setattr(
        pipeline, "registry_sources", lambda payload: pipeline.normalize_sources(sources)
    )
    batcher = pipeline.source_batches
    monkeypatch.setattr(pipeline, "source_batches", lambda value: batcher(value, limit=7000))
    # Preserve the fixture numbering; selection/remapping has a separate grounded test below.
    monkeypatch.setattr(
        pipeline, "select_pdf_sources", lambda s, f, q: (s, f, {n: n for n in range(1, 6)})
    )
    calls, renders = [], []
    scenario = gold["scenario"]
    authored = {
        "situation": gold["situation"],
        "assessment": {key: gold[key] for key in ("priority", "diagnosis")},
        "scenario": {
            "triggers": scenario["levels"][0]["risks"],
            "risk_titles": {
                str(n): [r["title"] for r in scenario["levels"][n - 1]["risks"]] for n in (2, 3)
            },
            "takeaway": scenario["price"]["takeaway"],
            "subtitle": scenario["subtitle"],
        },
        "offer": gold["offer"],
    }

    def invoke(_client, _url, _headers, data, label):
        stage = label.removeprefix("Отчёт: ").split(":")[0]
        calls.append(stage)
        if stage == "facts":
            source_numbers = {
                s["n"] for s in json.loads(data["messages"][1]["content"])["data"]["sources"]
            }
            value = {"facts": [f for f in facts if f["source_n"] in source_numbers]}
        elif stage == "research":
            value = research
        elif stage == "critic":
            # A draft must not have been published before independent approval.
            assert not store.saved and not (store.root / "data/local/report-pdfs").exists()
            value = {
                "checks": [
                    {
                        "criterion": i,
                        "passed": critic_pass,
                        "quote": "" if critic_pass else "Срок",
                        "reason": "" if critic_pass else "Уточнить формулировку",
                    }
                    for i in range(1, 10)
                ]
            }
        else:
            value = authored[stage]
        return response(value, tool="tools" in data)

    def renderer(value):
        renders.append(value)
        ReportV5.model_validate(value)
        return b"%PDF-offline", [{"name": "offer", "over_px": 3}] if overflow else []

    result = pipeline.generate(
        store,
        "record",
        "job",
        client=object(),
        invoke=invoke,
        renderer=renderer,
        researcher=lambda *a, **kw: None,
    )
    return result, calls, renders


def test_full_pipeline_publishes_only_reviewed_v5(harness, materials, monkeypatch):
    result, calls, renders = execute_full(harness, materials, monkeypatch)
    payload = result["payload"]
    assert payload["report_pipeline"]["passed"] is True, payload["report_pipeline"].get("issues")
    assert payload["report_v2"]["meta"]["schema_version"] == "5"
    assert calls[-1] == "critic" and "approach" not in calls and len(renders) == 1
    assert (
        harness[0].root / f"data/local/report-pdfs/{result['id']}.pdf"
    ).read_bytes() == b"%PDF-offline"


@pytest.mark.parametrize("cause", ["critic", "overflow"])
def test_failed_quality_gate_has_two_repairs_and_no_pdf(harness, materials, monkeypatch, cause):
    result, calls, renders = execute_full(
        harness, materials, monkeypatch, critic_pass=cause != "critic", overflow=cause == "overflow"
    )
    assert result["payload"]["report_pipeline"]["passed"] is False
    assert result["payload"]["report_pipeline"]["status"] == "manual"
    assert result["payload"]["report_pipeline"]["repairs"] == 2
    # Identical failed revisions reuse the independent verdict instead of rebilling it.
    assert len(result["payload"]["report_pipeline"]["checks"]) == 3
    assert calls.count("critic") == 1
    assert len(renders) == (3 if cause == "overflow" else 0)
    assert not (harness[0].root / "data/local/report-pdfs").exists()


def test_gateway_error_does_not_spend_editorial_retries(harness, materials, monkeypatch):
    store, _ = harness
    monkeypatch.setattr(pipeline, "registry_sources", lambda payload: materials["sources"])
    calls = []

    def invoke(*args):
        calls.append(args)
        raise ValueError("Отчёт: facts: сервис моделей отклонил подключение (403)")

    with pytest.raises(pipeline.ModelCallError, match="403"):
        pipeline.generate(
            store, "record", "job", client=object(), invoke=invoke, researcher=lambda *a, **kw: None
        )
    assert len(calls) == 1 and store.saved == []


def test_select_pdf_sources_preserves_passport_and_current_stage(materials):
    sources = pipeline.normalize_sources(materials["sources"])
    facts = pipeline.ResearchFacts(facts=materials["facts"])
    qualification = qualify(materials["research"], sources, facts, as_of="2026-11-16")
    selected, remapped, mapping = pipeline.select_pdf_sources(sources, facts, qualification)
    assert set(mapping) == {1, 2, 3, 4}
    assert selected[0]["registry_n"] == 4
    assert set(f.source_n for f in remapped.facts) == {1, 2, 3, 4}
    assert [s["n"] for s in selected] == list(range(1, len(selected) + 1))


def test_registry_over_limit_is_saved_for_manual_decision(harness, materials, monkeypatch):
    store, _ = harness
    sources = [dict(materials["sources"][0], n=n) for n in range(1, 42)]
    monkeypatch.setattr(pipeline, "registry_sources", lambda payload: sources)
    result = pipeline.generate(
        store,
        "record",
        "job",
        client=object(),
        invoke=lambda *args: pytest.fail("The over-limit registry must not call a model"),
        researcher=lambda *a, **kw: None,
    )
    assert result["payload"]["report_pipeline"]["status"] == "manual"
    assert len(result["payload"]["report_pipeline"]["sources"]) == 41
    assert not (store.root / "data/local/report-pdfs").exists()


def test_aggregate_facts_overflow_saves_registry_without_truncation(harness, monkeypatch):
    store, _ = harness
    sources, by_n = [], {}
    for n in range(1, 14):
        facts = [
            {
                "text": f"Факт {n}-{i}",
                "verbatim": f"Факт {n}-{i}",
                "date": None,
                "source_n": n,
                "who_says": "источник",
                "kind": "claim",
            }
            for i in range(24)
        ]
        source = {
            "n": n,
            "url": f"https://example.com/{n}",
            "text": "\n".join(f["text"] for f in facts),
        }
        sources.append(source)
        by_n[n] = facts
    monkeypatch.setattr(pipeline, "registry_sources", lambda payload: sources)
    monkeypatch.setattr(pipeline, "source_batches", lambda value: ([s] for s in value))

    def invoke(_client, _url, _headers, data, _label):
        n = json.loads(data["messages"][1]["content"])["data"]["sources"][0]["n"]
        return response({"facts": by_n[n]})

    result = pipeline.generate(
        store, "record", "job", client=object(), invoke=invoke, researcher=lambda *a, **kw: None
    )
    assert result["payload"]["report_pipeline"]["status"] == "manual"
    assert len(result["payload"]["report_pipeline"]["sources"]) == 13
    assert "300" in result["payload"]["report_pipeline"]["issues"][0]
    assert not (store.root / "data/local/report-pdfs").exists()


def test_snippets_and_access_pages_stay_in_registry_but_are_not_extracted():
    sources = [
        {"n": 1, "text": "Текст поискового сниппета", "kind": "snippet"},
        {"n": 2, "text": "Подтвердите, что вы не робот", "title": "Проверяем браузер"},
        {"n": 3, "text": "Прочитанный материал о сроках", "title": "Ход строительства"},
    ]
    batches = list(pipeline.source_batches(sources))
    assert [s["n"] for batch in batches for s in batch] == [3]
    assert len(sources) == 3


def test_grounding_feedback_reports_all_bad_fact_paths(materials):
    facts = pipeline.FactsBatch(facts=materials["facts"][:2])
    facts.facts[0].verbatim = "Перефраз вместо первого исходного фрагмента"
    facts.facts[1].source_n = 99
    with pytest.raises(ValueError) as captured:
        pipeline.ground_facts(facts, materials["sources"])
    details = json.loads(str(captured.value))
    assert {i["path"] for i in details["fact_errors"]} >= {"facts/0/verbatim", "facts/1/source_n"}


def test_same_model_failure_does_not_spend_a_second_identical_repair(harness, materials):
    store, _ = harness
    calls = []

    def invoke(*_args):
        calls.append(1)
        return response({"facts": materials["facts"]})

    runner = pipeline.Runner(store, "job", object(), invoke)
    with pytest.raises(pipeline.NeedsReview, match="повторила"):
        runner.step(
            "facts:0",
            pipeline.FactsBatch,
            "Факты",
            {},
            "extract",
            lambda value: (_ for _ in ()).throw(ValueError("facts/1/verbatim: нет фрагмента")),
        )
    assert len(calls) == 2
    assert runner.repairs == 1


def test_twenty_valid_atoms_survive_two_invalid_without_paid_retry(harness):
    store, _ = harness
    valid = [
        {
            "text": f"Подтверждённый факт {n}",
            "date": None,
            "source_n": 1,
            "who_says": "источник",
            "kind": "claim",
            "verbatim": f"Подтверждённый факт {n}",
        }
        for n in range(20)
    ]
    sources = [{"n": 1, "text": "\n".join(f["verbatim"] for f in valid)}]
    invalid = [
        dict(valid[0], verbatim="Полностью выдуманный исходный фрагмент"),
        dict(valid[1], text="Количество домов — 999"),
    ]
    calls, issues = [], []

    def invoke(*_args):
        calls.append(1)
        return response({"facts": valid + invalid})

    runner = pipeline.Runner(store, "job", object(), invoke)
    result = runner.step(
        "facts:0",
        pipeline.FactsBatch,
        "Извлеки факты",
        {},
        "extract",
        lambda value: pipeline.ground_facts(value, sources, issues=issues),
    )
    assert [f.model_dump() for f in result.facts] == valid
    assert len(calls) == 1 and runner.repairs == 0
    assert {i["path"] for i in issues} == {"facts/20/verbatim", "facts/21"}
    assert all(i["source_n"] == 1 and i["fact"] in invalid for i in issues)
    assert sources[0]["text"] == "\n".join(f["verbatim"] for f in valid)


def test_empty_and_fully_rejected_batches_need_no_schema_repair(harness):
    store, _ = harness
    runner = pipeline.Runner(store, "job", object(), lambda *_args: response({"facts": []}))
    result = runner.step("facts:0", pipeline.FactsBatch, "Факты", {}, "extract")
    assert result.facts == [] and runner.repairs == 0
    with pytest.raises(ValueError):
        pipeline.ResearchFacts(facts=[])


def test_rejected_atoms_are_saved_internally_with_registry_and_no_rebilling(harness, monkeypatch):
    store, _ = harness
    sources = [{"n": 1, "url": "https://example.com/object", "text": "Исходный материал"}]
    monkeypatch.setattr(pipeline, "registry_sources", lambda payload: sources)
    calls = []

    def invoke(*_args):
        calls.append(1)
        return response(
            {
                "facts": [
                    {
                        "text": "Иное утверждение",
                        "source_n": 1,
                        "date": None,
                        "kind": "claim",
                        "who_says": "источник",
                        "verbatim": "Несуществующая цитата",
                    }
                ]
            }
        )

    result = pipeline.generate(
        store, "record", "job", client=object(), invoke=invoke, researcher=lambda *a, **kw: None
    )
    saved = result["payload"]["report_pipeline"]
    assert saved["status"] == "manual" and saved["repairs"] == 0
    assert len(calls) == 1 and saved["sources"] == sources
    assert saved["facts"] == {"facts": []}
    assert saved["extraction_issues"][0]["path"] == "facts/0/verbatim"
    assert saved["extraction_issues"][0]["source_n"] == 1
    assert saved["extraction_issues"][0]["batch"] == 0
    assert "report_v2" not in result["payload"]


def test_ui_progress_contains_safe_text_not_validation_payload(harness, monkeypatch):
    store, _ = harness
    progress = []
    monkeypatch.setattr(pipeline, "report_progress", lambda *args: progress.append(args))
    outputs = iter([{"facts": [{"bad": "json"}]}, {"facts": []}])
    runner = pipeline.Runner(store, "job", object(), lambda *_args: response(next(outputs)))
    runner.step("facts:0", pipeline.FactsBatch, "Факты", {}, "extract")
    assert runner.repairs == 1
    assert all(args[2] == 1 for args in progress)
    assert progress[-1][-1] == "Сверяем факты и уточняем формулировки"
    assert "json" not in progress[-1][-1]


def test_typed_bad_atom_is_discarded_before_batch_validation_without_repair(harness):
    store, _ = harness
    valid = [
        {
            "text": f"Факт об объекте {n}",
            "date": None,
            "source_n": 1,
            "who_says": "источник",
            "kind": "claim",
            "verbatim": f"Факт об объекте {n}",
        }
        for n in range(20)
    ]
    invalid_typed = dict(valid[0], date="2026-11-16 и ещё одна дата с объяснением причин переноса")
    invalid_grounded = dict(valid[0], verbatim="Неподтверждённая подмена цитаты")
    raw = valid[:7] + [invalid_typed] + valid[7:] + [invalid_grounded]
    sources = [{"n": 1, "text": "\n".join(f["verbatim"] for f in valid)}]
    issues, calls = [], []

    def invoke(*_args):
        calls.append(1)
        return response({"facts": raw})

    runner = pipeline.Runner(store, "job", object(), invoke)
    result = runner.step(
        "facts:0",
        pipeline.FactsBatch,
        "Факты",
        {},
        "extract",
        lambda parsed: pipeline.filter_fact_batch(parsed, sources, issues=issues, batch=0),
        parser=lambda result: pipeline.parse_fact_batch(result, issues=issues, batch=0),
    )
    assert [f.model_dump() for f in result.facts] == valid
    assert len(calls) == 1 and runner.repairs == 0
    assert {i["path"] for i in issues} == {"facts/7/date", "facts/21/verbatim"}
    assert next(i for i in issues if i["path"] == "facts/7/date")["fact"] == invalid_typed
    assert next(i for i in issues if i["path"] == "facts/21/verbatim")["fact"] == invalid_grounded


def test_messages_are_not_duplicated_in_api_batches():
    messages = [{"text": "Сообщение из чата", "date": "2026-11-16"}]
    source = {
        "n": 1,
        "text": "Сообщение из чата",
        "messages": messages,
        "message_dates": ["2026-11-16"],
        "url": "https://t.me/example",
    }
    batch = list(pipeline.source_batches([source]))[0][0]
    assert "messages" not in batch
    assert batch["text"] == source["text"] and batch["message_dates"] == source["message_dates"]
    assert source["messages"] == messages


@pytest.mark.parametrize(
    "role,expected",
    [("author", "tool"), ("extract", "json_schema"), ("review", "json_schema")],
)
def test_response_format_is_selected_by_configured_role_not_model_id(harness, role, expected):
    store, _ = harness
    bodies = []

    def invoke(_client, _url, _headers, body, _label):
        bodies.append(body)
        return response(
            {"edits": [{"path": "text", "text": "Готовый текст"}]}, tool="tools" in body
        )

    runner = pipeline.Runner(store, "job", object(), invoke)
    runner.models[role]["model"] = "arbitrary/provider-model"
    runner.step("outbound-test", pipeline.schema.TextEdits, "Текст", {}, role)
    if expected == "tool":
        assert "response_format" not in bodies[0]
        assert bodies[0]["tool_choice"]["function"]["name"] == "return_report_data"
        function = bodies[0]["tools"][0]["function"]
        assert (
            "strict" not in function
            and function["parameters"] == pipeline.schema.TextEdits.model_json_schema()
        )
    else:
        assert bodies[0]["response_format"]["type"] == expected
    assert "TextEdits" in bodies[0]["messages"][0]["content"]


def test_usp_uses_same_project_short_name_and_verified_aliases_without_accepting_other_projects():
    official = "ЖК «Светлый мир „Станция Л“»"
    assert pipeline.project_short_name(official) == "Станция Л"
    context = {
        "object": {"name": official, "short_name": pipeline.project_short_name(official)},
        "qualification": {"passport": {"aliases": ["ЖК Станция Л", "корпус 1"]}},
    }
    assert pipeline.usp_mentions_object("«Станция Л»: снизим риск повторных обращений", context)
    assert pipeline.usp_mentions_object("СтанцияЛ: единая позиция для дольщиков", context)
    assert not pipeline.usp_mentions_object("Другой квартал: работа с дольщиками", context)
    assert not pipeline.usp_mentions_object("Корпус 1: работа с дольщиками", context)
    assert not pipeline.usp_mentions_object("Станция Лесная: работа с дольщиками", context)
    context = {"object": {"name": "ЖК Намёткин Тауэр", "short_name": "Намёткин Тауэр"}}
    assert pipeline.usp_mentions_object("Наметкин Тауэр: единая позиция", context)


def test_pdf_projection_keeps_highest_stage_and_has_one_citation_namespace():
    sources = [
        {
            "n": n,
            "url": f"https://example.com/{n}",
            "text": f"Материал источника {n}",
            "contour": "official" if n <= 5 else "media",
            "evidence_date": "2026-11-01",
        }
        for n in range(1, 11)
    ]
    facts = pipeline.ResearchFacts(
        facts=[
            {
                "text": s["text"],
                "verbatim": s["text"],
                "date": "2026-11-01",
                "source_n": s["n"],
                "kind": "claim",
                "who_says": "источник",
            }
            for s in sources
        ]
    )
    qualification = {
        "verdict": "report_full",
        "client_role": "застройщик",
        "current_stage": 4,
        "passport": {
            "official_name": "ЖК Тест",
            "status_refs": [1],
            "status_fact_indexes": [0],
            "addressee_refs": [2],
            "developer_refs": [3],
            "developer_status_refs": [4],
            "phases": [{"name": "Корпус 1", "refs": [1, 10]}],
        },
        "stage_signs": [
            {"stage": 4, "source_n": 9, "fact_indexes": [8], "text": "Публичная акция"}
        ],
        "problem_registry": [
            {
                "id": f"p{n}",
                "name": f"Проблема {n}",
                "topic": "communication",
                "in_scope": True,
                "status": "active",
                "source_numbers": [n],
                "evidence_ids": [f"e{n}"],
                "evidence_count": 20,
                "publishers": ["Широкий реестр"],
            }
            for n in (7, 8, 9)
        ],
        "research_evidence": {"evidence": [{"source_n": 10, "fact_indexes": [9]}]},
        "coverage": {"dated_evidence_12m": 20},
    }
    original = copy.deepcopy(qualification)
    selected, pdf_facts, mapping = pipeline.select_pdf_sources(sources, facts, qualification)
    assert len(selected) == 6 and selected[0]["registry_n"] == 9
    assert {1, 2, 3, 4, 9} <= mapping.keys()
    projected = pipeline.author_qualification(qualification, mapping)
    assert qualification == original
    assert "research_evidence" not in projected and "coverage" not in projected
    assert projected["stage_signs"][0]["source_n"] == mapping[9]
    assert all(set(p["source_numbers"]) <= set(range(1, 7)) for p in projected["problem_registry"])
    assert len(projected["problem_registry"]) == 2
    for node in pipeline.walk(projected):
        assert not any(k == "fact_indexes" or k.endswith("_fact_indexes") for k in node)
        for key, refs in node.items():
            if key.endswith("refs"):
                assert set(refs) <= set(range(1, 7))
    assert {f.source_n for f in pdf_facts.facts} == set(range(1, 7))
    assert (
        next(s for s in selected if s["n"] == projected["stage_signs"][0]["source_n"])["registry_n"]
        == 9
    )
