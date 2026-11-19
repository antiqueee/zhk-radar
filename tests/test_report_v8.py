import json
from pathlib import Path
from shutil import copytree

import pytest
import test_report_pipeline_v5 as fixtures
from test_report_research_anchors import native_payload

from radar.reports import pipeline_v8 as pipeline
from radar.reports.render import build_html, is_v8, page_count
from radar.reports.schema import TextEdits
from radar.reports.schema_v8 import ReportV8
from radar.reports.v8 import check
from radar.web.store import Store

ROOT = Path(__file__).resolve().parents[1]


def test_rerun_reuses_valid_model_output_and_refetches_unreadable_replies(monkeypatch, tmp_path):
    copytree(ROOT / "config", tmp_path / "config")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    store = Store(tmp_path)
    valid = {"edits": [{"path": "text", "text": "Исправленный текст"}]}
    replies = iter([fixtures.response(valid), {"choices": []}, {"choices": []},
                    fixtures.response(valid)])
    stages = []

    def invoke(_client, _url, _headers, _body, label):
        stages.append(label)
        return next(replies)

    def stage(runner, name):
        return runner.step(name, TextEdits, "Проверка", {}, "extract")

    first = pipeline.Runner(store, "first", None, invoke)
    assert stage(first, "ready").model_dump() == valid
    with pytest.raises(pipeline.InvalidModelOutput):
        stage(first, "broken")

    second = pipeline.Runner(store, "second", None, invoke)
    assert stage(second, "ready").model_dump() == valid
    assert stage(second, "broken").model_dump() == valid
    assert stages == ["Отчёт: ready", "Отчёт: broken", "Отчёт: broken", "Отчёт: broken"]


def test_v8_prose_targets_do_not_reject_copy_or_change_historical_v5():
    from pydantic import ValidationError

    from radar.reports import schema_v5

    data = json.loads((ROOT / "src/radar/reports/gold/v8/nametkin-tower.json").read_text())
    data["situation"]["lead"] = "Дольщики ожидают передачи помещений. " * 10
    data["situation"]["buyers"]["note"] = "Вопрос о сроках передачи остаётся актуальным. " * 5
    data["situation"]["company"]["points"][0]["text"] = (
        "Компания сообщает о ходе работ и подготовке к передаче помещений. " * 4
    )
    data["offer"]["modules"][0]["agency"][0] = (
        "Готовим согласованное сообщение дольщикам и порядок дальнейших контактов. " * 2
    )
    # Five variants also have no hidden 45-character rejection threshold.
    five = next(m for m in data["offer"]["modules"] if len(m["variants"]) == 5)
    five["variants"][0]["text"] = "Готовим понятное сообщение и согласуем порядок обратной связи."
    result = ReportV8.model_validate(data).model_dump()
    assert result["situation"]["lead"] == data["situation"]["lead"]
    assert check(result) == []
    with pytest.raises(ValidationError, match="String should have at most"):
        schema_v5.Point(text=data["situation"]["company"]["points"][0]["text"], refs=[1])


def test_v8_relaxed_copy_keeps_evidence_and_structure_validation():
    data = json.loads((ROOT / "src/radar/reports/gold/v8/nametkin-tower.json").read_text())
    data["situation"]["timeline"][0]["date_iso"] = "неизвестно"
    data["diagnosis"]["sources"][0]["n"] = 7
    data["priority"]["criteria"][0]["score"] = 4
    data["offer"]["modules"] = data["offer"]["modules"][:1]
    with pytest.raises(ValueError):
        ReportV8.model_validate(data)
    assert any("контракт" in issue for issue in check(data))


@pytest.mark.parametrize("name", ["nametkin-tower", "stantsiya-l"])
def test_v8_gold_contract_checks_and_six_complete_pages(name):
    data = ReportV8.model_validate_json(
        (ROOT / f"src/radar/reports/gold/v8/{name}.json").read_text()
    ).model_dump()
    assert check(data) == [] and is_v8(data)
    assert build_html(data).count('class="page') == page_count(data) == 6
    if name == "nametkin-tower":
        assert "Дополнительная зона риска" in build_html(data)
        assert [len(m["variants"]) for m in data["offer"]["modules"]] == [0, 5, 3, 3]


@pytest.mark.parametrize(
    "has_matrix_match,revision",
    [
        (False, "pass"),
        (True, "pass"),
        (True, "offer"),
        (True, "critic"),
        (True, "critic_rejects"),
        (True, "layout"),
        (True, "persistent_overflow"),
        (True, "metric_format"),
        (True, "authority_scope"),
        (True, "editorial_scope"),
    ],
)
def test_v8_engine_authors_matrix_cards_and_publishes_only_after_independent_check(
    monkeypatch, tmp_path, has_matrix_match, revision
):
    monkeypatch.setattr(fixtures, "pipeline", pipeline)
    store, payload = fixtures.harness.__wrapped__(monkeypatch, tmp_path)
    materials = fixtures.materials.__wrapped__()
    old, sources, facts, research = fixtures.full_inputs(materials)
    original_facts = list(facts)
    gold = json.loads((ROOT / "src/radar/reports/gold/v8/nametkin-tower.json").read_text())
    gold["situation"]["lead"] = "Дольщики ожидают передачи помещений. " * 8
    gold["situation"]["buyers"]["note"] = "Дольщики обсуждают сроки передачи помещений. " * 4
    gold["diagnosis"]["extra_risk"] = None
    gold["diagnosis"]["main_problem"]["level"] = "Длинное описание причины оценки"
    gold["diagnosis"]["main_problem"]["scale_note"] = "неточная подпись"
    if revision == "metric_format":
        gold["diagnosis"]["main_problem"]["score"] = 6
    gold["diagnosis"]["sources"] = old["diagnosis"]["sources"]
    gold["diagnosis"]["stage"]["signs"] = old["diagnosis"]["stage"]["signs"]
    gold["diagnosis"]["main_problem"]["causes"] = old["diagnosis"]["main_problem"]["causes"]
    gold["situation"]["buyers"]["topics"] = [gold["situation"]["buyers"]["topics"][0]]
    # The author now chooses immutable quote keys; this integration fixture uses
    # supported topics. Exact quote insertion has independent source-span tests.
    gold["situation"]["buyers"]["quotes"] = []
    gold["offer"]["modules"] = [gold["offer"]["modules"][0], gold["offer"]["modules"][2]]
    for node in pipeline.walk(gold):
        for key, refs in list(node.items()):
            if key.endswith("refs"):
                node[key] = [n for n in refs if n <= 5]
    proof = json.dumps({k: gold[k] for k in ("situation", "diagnosis")}, ensure_ascii=False)
    for source in sources:
        source["text"] += "\n" + proof
        for offset in range(0, len(proof), 850):
            facts.append(
                {
                    "text": "Материалы об объекте",
                    "date": source["published_at"],
                    "source_n": source["n"],
                    "who_says": "источник",
                    "kind": "claim",
                    "verbatim": proof[offset : offset + 850],
                }
            )
    for event in gold["situation"]["timeline"]:
        fact = {
            "text": event["text"],
            "date": event["date_iso"],
            "source_n": event["refs"][0],
            "who_says": "источник",
            "kind": "claim",
            "verbatim": event["text"],
        }
        if fact not in facts:
            facts.append(fact)
    research["problems"][0]["name"] = "Перенос сроков"
    ordered = [fact for n in range(1, 6) for fact in facts if fact["source_n"] == n]
    by_fact = {json.dumps(fact, sort_keys=True): index for index, fact in enumerate(ordered)}
    for item in [*research["evidence"], *research["stage_signs"]]:
        item["fact_indexes"] = [
            by_fact[json.dumps(original_facts[i], sort_keys=True)] for i in item["fact_indexes"]
        ]
    for key in list(research["passport"]):
        if key.endswith("_fact_indexes"):
            research["passport"][key] = []
    payload.update(
        name=gold["object"]["name"],
        developer=gold["object"]["developer"],
        monitoring=None if revision == "metric_format" else {"score": 8, "date": "2026-11-05"},
    )
    monkeypatch.setattr(pipeline, "registry_sources", lambda _: pipeline.normalize_sources(sources))
    original_batches = pipeline.source_batches
    monkeypatch.setattr(
        pipeline, "source_batches", lambda value: original_batches(value, limit=15000)
    )
    monkeypatch.setattr(
        pipeline, "select_pdf_sources", lambda s, f, q: (s, f, {n: n for n in range(1, 6)})
    )
    scenario = gold["scenario"]
    authored = {
        "situation": gold["situation"],
        "assessment": {k: gold[k] for k in ("priority", "diagnosis")},
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
    calls, rendered = [], []

    def invoke(_client, _url, _headers, body, label):
        stage = label.removeprefix("Отчёт: ").split(":")[0]
        calls.append(stage)
        if stage == "facts":
            numbers = {
                s["n"] for s in json.loads(body["messages"][1]["content"])["data"]["sources"]
            }
            value = {"facts": [f for f in facts if f["source_n"] in numbers]}
        elif stage == "research":
            normalized = pipeline.normalize_research(
                research, payload, sources, ordered, "2026-11-16"
            ).model_dump()
            value = native_payload(normalized, ordered)
            first = value["evidence"][0]
            registry = pipeline.research_anchors.fact_registry(ordered)
            source_n = registry[first["anchor_fact_key"]]["fact"]["source_n"]
            foreign = next(
                key for key, row in registry.items() if row["fact"]["source_n"] != source_n
            )
            first["supporting_fact_keys"].extend([foreign, "f9999-missing"])
        elif stage == "matrix":
            value = {
                "problems": [
                    {"problem_id": p["id"], "matrix_id": "Q02" if has_matrix_match else None}
                    for p in research["problems"]
                ],
                "tactics": [],
                "accelerators": [],
                "factors": [],
            }
        elif stage == "copy_edit" and revision == "authority_scope":
            assert label == "Отчёт: copy_edit:offer"
            assert not rendered
            edits = json.loads(body["messages"][1]["content"])["data"]["edits"]
            by_path = {edit["path"]: edit for edit in edits}
            assert set(by_path) == {"offer.modules.0.pain", "offer.usp"}
            assert by_path["offer.modules.0.pain"]["cited_refs"] == (
                authored["offer"]["modules"][0]["pain_refs"]
            )
            assert [row["n"] for row in by_path["offer.modules.0.pain"]["cited_sources"]] == (
                by_path["offer.modules.0.pain"]["cited_refs"]
            )
            assert "срок выдачи ключей допустим" in by_path["offer.usp"]["instruction"].casefold()
            value = {"edits": [
                {"path": "offer.modules.0.pain", "text": authored["offer"]["modules"][0]["pain"]},
                {"path": "offer.usp", "text": authored["offer"]["usp"]},
            ]}
        elif stage == "copy_edit" and revision == "editorial_scope":
            assert not rendered
            edit = json.loads(body["messages"][1]["content"])["data"]
            if label == "Отчёт: copy_edit:scenario":
                original = authored["scenario"]["triggers"][0]
                assert edit["path"] == "scenario.triggers.0.text"
                assert edit["cited_refs"] == original["refs"]
                value = {"text": original["text"]}
            else:
                assert label == "Отчёт: copy_edit:offer"
                original = authored["offer"]["modules"][0]
                assert edit["path"] == "offer.modules.0.pain"
                assert edit["cited_refs"] == original["pain_refs"]
                value = {"text": original["pain"]}
        elif stage == "critic":
            assert not store.saved
            assert rendered
            fields = json.loads(body["messages"][1]["content"])["data"]["report_field_options"]
            value = {
                "checks": [
                    {"criterion": n, "passed": True, "field_key": None, "reason": "Проверено"}
                    for n in range(1, 10)
                ]
            }
            if revision == "critic_rejects" or (
                calls.count("critic") == 1 and revision in {"offer", "critic"}
            ):
                value["checks"][5].update(
                    passed=False,
                    field_key=next(
                        row["field_key"] for row in fields if row["path"] == "/offer/usp"
                    )
                    if revision in {"offer", "critic_rejects"} else "r_missing",
                    reason="Уточните механизм предложения"
                    if revision in {"offer", "critic_rejects"}
                    else "Этот текст критик ошибочно взял из каталога",
                )
        else:
            value = authored[stage]
            contract_change = "Дольщики получили изменённые условия по ДДУ."
            if stage == "scenario" and revision == "editorial_scope":
                first, *rest = value["triggers"]
                value = {**value, "triggers": [{**first, "text": contract_change}, *rest]}
            if stage == "offer" and revision == "editorial_scope":
                first, *rest = value["modules"]
                value = {**value, "modules": [{**first, "pain": contract_change}, *rest]}
            if stage == "offer" and revision == "authority_scope":
                first, *rest = value["modules"]
                value = {
                    **value,
                    "usp": f"{gold['object']['name']}: сдержим эскалацию до декабря 2027 года.",
                    "modules": [{**first, "pain": "Ведомство получило жалобу дольщиков."}, *rest],
                }
            if stage == "situation" and revision == "metric_format":
                assert "assessment" not in calls
                if calls.count("situation") == 1:
                    tiles = value["facts"]
                    value = {
                        **value,
                        "facts": [{**tiles[0], "value": "Ключи переносятся"}, *tiles[1:]],
                    }
                else:
                    inputs = json.loads(body["messages"][1]["content"])
                    assert inputs["feedback"]["path"] == "situation.facts.0.value"
            if stage == "offer" and calls.count("offer") > 1:
                if revision in {"layout", "persistent_overflow"}:
                    inputs = json.loads(body["messages"][1]["content"])["data"]
                    assert inputs["revision"]["pages"] == ["offer"]
                    assert inputs["revision"]["issues"] == overflow
                    assert inputs["previous_page"]["usp"] == value["usp"]
                    value = {
                        **value,
                        "usp": f"{gold['object']['name']}: согласуем маршрут обращений.",
                    }
                else:
                    value = {**value, "usp": value["usp"] + " Согласуем маршрут обращения."}
        return fixtures.response(value, tool="tools" in body)

    overflow = [
        {"page": 5, "name": "solutions-1", "block": "body", "kind": "height", "over_px": 90}
    ]

    def renderer(value):
        assert not check(value)
        if revision == "authority_scope":
            assert value["offer"]["modules"][0]["pain"] == authored["offer"]["modules"][0]["pain"]
            assert value["offer"]["usp"] == authored["offer"]["usp"]
        rendered.append(value)
        if revision == "persistent_overflow" or (revision == "layout" and len(rendered) == 1):
            assert "critic" not in calls
            return b"", overflow
        return b"%PDF-v8-reviewed", []

    result = pipeline.generate(
        store,
        "record",
        "job",
        client=object(),
        invoke=invoke,
        renderer=renderer,
        researcher=lambda *a, **kw: None,
    )
    if not has_matrix_match:
        assert result["payload"]["report_pipeline"]["status"] == "manual"
        assert not rendered and calls[-1] == "matrix"
        assert calls.count("matrix") == 1 and "offer" not in calls and "situation" not in calls
        assert result["payload"]["report_pipeline"]["repairs"] == 0
        assert result["payload"]["report_qualification"]["matrix_matching"]["problems"]
        assert not result["payload"]["report_pipeline"]["passed"]
        return
    if revision == "persistent_overflow":
        audit = result["payload"]["report_pipeline"]
        assert audit["status"] == "manual"
        assert audit["overflow"] == overflow
        assert audit["layout_repairs"] == 1
        assert not audit["passed"] and "critic" not in calls
        assert len(rendered) == 2 and calls.count("offer") == 2
        return
    if revision == "critic_rejects":
        audit = result["payload"]["report_pipeline"]
        assert audit["status"] == "manual" and not audit["passed"]
        assert audit["repairs_by_stage"]["review_revision"] == 2
        assert audit["checks"][-1]["issues"][0]["path"] == "/offer/usp"
        assert audit["checks"][-1]["issues"][0]["criterion"] == 6
        with store.connect() as db:
            history = json.loads(db.execute("SELECT payload FROM brief_reviews").fetchone()[0])
        assert history["verdict"] == "manual"
        assert history["issues"][0]["block"] == "/offer/usp"
        assert history["issues"][0]["reason"] == "Уточните механизм предложения"
        return
    assert result["payload"]["report_pipeline"]["passed"], (
        result["payload"]["report_pipeline"]["issues"],
        result["payload"]["report_qualification"].get("reasons"),
        result["payload"]["report_qualification"].get("manual_checks"),
        calls,
    )
    assert result["payload"]["report_v2"]["meta"]["schema_version"] == "8"
    main_problem = result["payload"]["report_v2"]["diagnosis"]["main_problem"]
    if revision == "metric_format":
        assert (main_problem["score"], main_problem["tone"], main_problem["level"]) == (
            6, "medium", "Средняя"
        )
        assert main_problem["scale_note"] == "оценка по открытым данным"
    else:
        assert main_problem["level"] == "Высокая"
        assert main_problem["scale_note"] == "оценка мониторинга чатов на 05.11.2026"
    assert calls.count("research") == 1
    assert result["payload"]["report_pipeline"]["repairs"] == {
        "pass": 0, "editorial_scope": 2,
    }.get(revision, 1)
    assert len(result["payload"]["report_pipeline"]["research_issues"]) == 2
    assert calls[-1] == "critic" and calls.count("matrix") == 1
    assert calls.count("situation") == (2 if revision == "metric_format" else 1)
    assert calls.count("assessment") == calls.count("scenario") == 1
    assert calls.count("offer") == (2 if revision in {"offer", "layout"} else 1)
    assert calls.count("critic") == (2 if revision in {"offer", "critic"} else 1)
    assert len(rendered) == (2 if revision in {"offer", "layout"} else 1)
    assert result["payload"]["report_pipeline"]["layout_repairs"] == int(revision == "layout")
    assert result["payload"]["report_pipeline"]["matrix_selection"]


def test_code_repairs_cannot_consume_the_content_review_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(fixtures, "pipeline", pipeline)
    store, _ = fixtures.harness.__wrapped__(monkeypatch, tmp_path)
    runner = pipeline.Runner(store, "job", None)
    runner.repair("editorial")
    runner.repair("editorial")
    runner.repair("review_revision")
    runner.repair("review_revision")
    with pytest.raises(pipeline.NeedsReview, match="критик снова отклонил"):
        runner.repair("review_revision")
    assert runner.repairs_by_stage == {"editorial": 2, "review_revision": 2}


@pytest.mark.parametrize(
    "field,changed",
    [
        ("monitoring_snapshot", {"score": 8, "date": "2026-11-05"}),
        ("manual_monitoring", {"score": 8, "date": "2026-11-05"}),
        ("monitoring_date", "2026-11-05"),
        ("report_scope", {"house_id": "66399", "phase": "II очередь"}),
    ],
)
def test_changed_monitoring_or_object_scope_does_not_return_an_old_saved_verdict(
    monkeypatch, tmp_path, field, changed
):
    monkeypatch.setattr(fixtures, "pipeline", pipeline)
    store, payload = fixtures.harness.__wrapped__(monkeypatch, tmp_path)
    materials = fixtures.materials.__wrapped__()
    sources, facts, research = materials["sources"], materials["facts"], materials["research"]
    for problem in research["problems"]:
        problem["status"] = "unknown"
    monkeypatch.setattr(pipeline, "registry_sources", lambda _: pipeline.normalize_sources(sources))

    def invoke(_client, _url, _headers, body, label):
        if label.startswith("Отчёт: facts:"):
            value = {"facts": facts}
        else:
            assert label == "Отчёт: research"
            value = native_payload(research, facts)
        return fixtures.response(value, tool="tools" in body)

    options = {"client": object(), "invoke": invoke, "researcher": lambda *a, **kw: None}
    first = pipeline.generate(store, "record", "job", **options)
    unchanged = pipeline.generate(store, "record", "job", **options)
    assert first["id"] == unchanged["id"] and len(store.saved) == 1
    assert first["payload"]["report_qualification"]["verdict"] == "watch"
    payload[field] = changed
    updated = pipeline.generate(store, "record", "job", **options)
    assert updated["id"] != first["id"] and len(store.saved) == 2
    assert not updated["payload"]["report_pipeline"]["passed"]


@pytest.mark.parametrize("retains_lower_sign", [False, True])
@pytest.mark.parametrize("stays_invalid", [False, True])
def test_research_repairs_rejected_signs_without_inventing_a_qualification(
    monkeypatch, tmp_path, retains_lower_sign, stays_invalid
):
    from copy import deepcopy

    monkeypatch.setattr(fixtures, "pipeline", pipeline)
    store, _ = fixtures.harness.__wrapped__(monkeypatch, tmp_path)
    materials = fixtures.materials.__wrapped__()
    sources, facts, research = materials["sources"], materials["facts"], materials["research"]
    for problem in research["problems"]:
        problem["status"] = "unknown"
    monkeypatch.setattr(pipeline, "registry_sources", lambda _: pipeline.normalize_sources(sources))
    requests = []

    def invoke(_client, _url, _headers, body, label):
        if label.startswith("Отчёт: facts:"):
            return fixtures.response({"facts": facts})
        assert label == "Отчёт: research"
        requests.append(json.loads(body["messages"][1]["content"]))
        candidate = deepcopy(research)
        if len(requests) == 1 or stays_invalid:
            candidate["stage_signs"][0]["text"] = "658 покупателей подписали обращение"
            if retains_lower_sign:
                candidate["stage_signs"].append({
                    "stage": 1, "text": "Дольщики ждут прямого ответа",
                    "date_iso": "2026-11-03", "source_n": 3, "fact_indexes": [2],
                })
        return fixtures.response(native_payload(candidate, facts), tool="tools" in body)

    result = pipeline.generate(
        store, "record", "job", client=object(), invoke=invoke,
        researcher=lambda *a, **kw: None,
    )
    assert len(requests) == 2
    assert (
        "признак более высокой стадии отклонён" if retains_lower_sign else
        "все признаки отклонены"
    ) in requests[1]["feedback"]
    assert json.loads(requests[1]["previous"])["stage_signs"][0]["text"].startswith("658")
    assert result["payload"]["report_pipeline"]["repairs_by_stage"] == {"research": 1}
    assert result["payload"]["report_qualification"]["verdict"] == "watch"
    if stays_invalid:
        assert result["payload"]["report_qualification"]["current_stage"] == (
            1 if retains_lower_sign else None
        )
    assert not result["payload"]["report_pipeline"]["passed"]
