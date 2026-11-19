import json
from pathlib import Path

import pytest
from jinja2 import UndefinedError
from pydantic import ValidationError

from radar.reports import schema
from radar.reports.pipeline import services, validate_facts, validate_offer
from radar.reports.render import build_html

ROOT = Path(__file__).resolve().parents[1]


def test_services_extend_existing_library():
    catalog, methods = services(ROOT)
    assert {f"S{i:02}" for i in range(1, 7)} <= {s["id"] for s in catalog}
    assert {f"O{i}" for i in range(1, 13)} <= {s["id"] for s in catalog}
    assert {m["id"] for m in methods} == {f"M{i:02}" for i in range(1, 16)}


def test_atomic_facts_reject_fabricated_quote_or_number():
    sources = [dict(n=1, text="Срок передачи — 2027 год.", published_at=None)]
    facts = schema.Facts(
        facts=[
            dict(
                text="Передача в 2027 году",
                date=None,
                source_n=1,
                who_says="публикация",
                kind="claim",
                verbatim=sources[0]["text"],
            )
        ]
    )
    validate_facts(facts, sources)
    changed = facts.model_copy(deep=True)
    changed.facts[0].text = "Передача в 2028 году"
    with pytest.raises(ValueError, match="число"):
        validate_facts(changed, sources)
    changed = facts.model_copy(deep=True)
    changed.facts[0].verbatim = "Все покупатели недовольны."
    with pytest.raises(ValueError, match="дословно"):
        validate_facts(changed, sources)


def test_message_and_critic_contracts_fail_closed():
    with pytest.raises(ValidationError, match="520"):
        schema.Message(greeting="Здравствуйте", paragraphs=["а" * 200] * 3, tips=["а"] * 3)
    with pytest.raises(ValidationError, match="девять"):
        schema.Critique(checks=[dict(criterion=1, passed=True, quote="", reason="")] * 9)
    with pytest.raises(ValidationError):
        schema.Module(
            name="а" * 23,
            service="Материалы",
            service_ids=["S02"],
            method_ids=[],
            pain="Ожидание",
            pain_refs=[1],
            solution="Ответы",
            steps=["а"] * 3,
            outcome="Ответы согласованы",
            metric="Повторные обращения",
        )


def test_strict_template_does_not_silently_print_missing_fields():
    with pytest.raises((UndefinedError, KeyError)):
        build_html({"diagnosis": {"sources": []}})


def test_monitoring_requires_found_community_and_archives_are_not_evidence():
    catalog, methods = services(ROOT)
    context = {
        "object": {"name": "ЖК Тест", "short_name": "ЖК Тест"},
        "sources": [{"n": 1}],
        "services": catalog,
        "methods": methods,
        "public_communities": [],
    }
    module = dict(
        name="Позиция",
        service="Подготовка материалов",
        service_ids=["S02"],
        method_ids=["M03"],
        pain="Перенос в ЖК Тест",
        pain_refs=[1],
        solution="Ответ по сроку",
        steps=["Памятка", "Встреча", "Протокол"],
        outcome="Согласован ответ",
        metric="Повторные вопросы",
    )
    value = dict(
        usp="ЖК Тест — согласованный ответ",
        usp_note="Объём согласуем",
        why_now=[dict(title="Срок", text="Ответы по сроку")] * 3,
        modules=[{**module, "pain": str(i) + " — перенос"} for i in range(3)],
    )
    validate_offer(schema.Offer.model_validate(value), context)
    value["modules"][0]["service_ids"] = ["S04"]
    with pytest.raises(ValueError, match="сообщества"):
        validate_offer(schema.Offer.model_validate(value), context)
    value["modules"][0]["service_ids"] = ["S02"]
    value["modules"][0]["pain_refs"] = [5]
    with pytest.raises(ValueError, match="источник"):
        validate_offer(schema.Offer.model_validate(value), context)


def analysis_fixture():
    return schema.Analysis.model_validate(
        {
            "priority": {
                "level": "high",
                "label": "Высокий",
                "criteria": [
                    {"name": n, "score": 1, "why": "Один материал"}
                    for n in ["Острота", "Публичность", "Окно для захода"]
                ],
            },
            "situation": {
                "lead": "Передача в 2027 году.",
                "facts": [
                    {"value": "2027", "label": "Срок передачи", "refs": [1], "tone": "neutral"}
                ]
                * 4,
                "timeline": [
                    {"date": "2027", "text": "План передачи", "refs": [1], "tone": "neutral"}
                ],
                "buyers": {"status": "Нет свидетельств", "note": "Чаты не прочитаны", "quotes": []},
                "company": {
                    "status": "Ответ не найден",
                    "points": [{"text": "Ответ не найден", "refs": []}],
                },
                "gap": "Требуется ответ",
            },
            "diagnosis": {
                "subtitle": "Предварительный разбор",
                "stage": {
                    "current": 1,
                    "confidence": "Предварительно",
                    "stages": [{"n": i, "name": "Стадия", "desc": "Признак"} for i in range(1, 5)],
                    "signs": [
                        {"text": "Срок передачи", "confirmed": True, "refs": [1]},
                        {"text": "Жалобы не подтверждены", "confirmed": False, "refs": []},
                    ],
                    "forecast": "Возможны вопросы покупателей",
                },
                "drivers": [
                    {
                        "title": "Срок",
                        "text": "Передача в 2027 году",
                        "confidence": "single",
                        "confidence_label": "Один источник",
                        "refs": [1],
                    }
                ],
                "stakeholders": [{"who": "Покупатели", "cares": "Сроки"}] * 3,
                "unknowns": [{"what": "Открытые чаты", "how": "Найти открытое сообщество"}] * 3,
                "sources": [
                    {
                        "n": 1,
                        "url": "https://example.com/1",
                        "publisher": "example.com",
                        "date": "2026",
                        "title": "Срок передачи",
                        "type": "page",
                    }
                ],
                "sources_note": "План не равен фактической передаче",
            },
        }
    )


def test_analysis_numbers_refs_and_priority_are_checked_in_code():
    from radar.reports.pipeline import ground_analysis

    facts = schema.Facts(
        facts=[
            {
                "text": "Передача в 2027 году",
                "date": None,
                "source_n": 1,
                "who_says": "застройщик",
                "kind": "claim",
                "verbatim": "Передача в 2027 году",
            }
        ]
    )
    sources = [{"n": 1, "url": "https://example.com/1", "text": "Передача в 2027 году"}]
    analysis = analysis_fixture()
    assert ground_analysis(analysis, facts, sources).priority.level == "low"
    analysis.situation.lead = "Передача в 2039 году"
    with pytest.raises(ValueError, match="2039"):
        ground_analysis(analysis, facts, sources)
    analysis = analysis_fixture()
    analysis.diagnosis.stage.signs[0].refs = []
    with pytest.raises(ValueError, match="признак"):
        ground_analysis(analysis, facts, sources)


def test_fit_editor_only_changes_reported_fields():
    from radar.reports.pipeline import Runner

    runner = object.__new__(Runner)
    captured = []

    def edit(stage, contract, prompt, context, role):
        captured.append(context)
        return schema.TextEdits(edits=[{"path": "text", "text": "Срок передачи"}])

    runner.step = edit
    original = {"date": "2027", "text": "Очень длинный текст " * 20, "refs": [1], "tone": "alert"}
    response = {
        "choices": [{"message": {"content": json.dumps(original)}, "finish_reason": "stop"}]
    }
    fixed = runner.parse_fitted(response, schema.Event, "diagnosis")
    assert fixed.date == "2027" and fixed.refs == [1] and fixed.tone == "alert"
    assert fixed.text == "Срок передачи" and captured[0]["fields"][0]["max_chars"] == 55


def test_compact_layout_is_computed_from_evidence_not_chosen_by_model():
    from radar.reports.pipeline import compose

    analysis = analysis_fixture()
    # compose accepts already validated stage objects, layout alone is under test.
    offer = schema.Offer.model_construct()
    approach = schema.Approach.model_construct()
    payload = {"name": "ЖК Тест", "developer": "Тест", "region": "Москва"}
    assert compose(payload, [{"n": 1}], analysis, offer, approach).meta.layout == "compact"
    analysis.diagnosis.drivers *= 3
    assert compose(payload, [{"n": 1}], analysis, offer, approach).meta.layout == "compact"
    assert compose(payload, [{"n": 1}, {"n": 2}], analysis, offer, approach).meta.layout == "full"


def test_report_accepts_word_numerals_and_verbatim_publisher_digits():
    from radar.reports.pipeline import ground_analysis

    source = "MSK1.RU: передача в 2027 году, задержка на девять месяцев."
    facts = schema.Facts(
        facts=[
            dict(
                text="Передача в 2027 году, задержка на девять месяцев.",
                date=None,
                source_n=1,
                who_says="Дольщики",
                kind="claim",
                verbatim=source,
            )
        ]
    )
    sources = [dict(n=1, url="https://example.com/1", text=source)]
    validate_facts(facts, sources)
    analysis = analysis_fixture()
    analysis.situation.lead = "MSK1.RU: передача в 2027 году, задержка 9 месяцев."
    ground_analysis(analysis, facts, sources)
    analysis.situation.lead = "Передача в 2027 году, задержка 18 месяцев."
    with pytest.raises(ValueError, match="18"):
        ground_analysis(analysis, facts, sources)


def test_short_year_in_full_date_preserves_same_numbers():
    from radar.reports.pipeline import numbers

    assert numbers("Ввод 31.03.27") == numbers("Ввод 31.03.2027")
    assert numbers("Задержка 27 месяцев") != numbers("Задержка 2027 месяцев")


def test_ready_draft_is_available_during_review_and_overflow_is_not_published(tmp_path):
    import shutil
    from types import SimpleNamespace

    from radar.reports.pipeline import save_downloadable_draft
    from radar.web.store import Store
    from radar.web.tasks import TaskRunner

    shutil.copytree(ROOT / "config", tmp_path / "config")
    store = Store(tmp_path)
    tasks = TaskRunner(store, {"author": lambda *args: None})
    job = tasks.submit("author", "demo-1")
    with store.connect() as db:
        db.execute("UPDATE object_tasks SET status='running' WHERE id=?", (job["id"],))
    report = SimpleNamespace(
        model_dump=lambda: {"meta": {"disclaimer": "Черновик"}}, offer=SimpleNamespace(modules=[])
    )
    args = (
        store,
        "demo-1",
        job["id"],
        {"report": {}, "cases": []},
        report,
        {"author": {"model": "author"}, "review": {"model": "review"}},
        b"%PDF-test",
    )
    assert save_downloadable_draft(*args, [{"over_px": 10}]) is None
    assert tasks.get(job["id"])["result"] is None
    saved = save_downloadable_draft(*args, [])
    task = tasks.get(job["id"])
    assert task["status"] == "running" and task["result"]["id"] == saved["id"]
    assert task["result"]["payload"]["author"]["status"] == "manual"
    assert (tmp_path / f"data/local/report-pdfs/{saved['id']}.pdf").read_bytes() == b"%PDF-test"


def test_offer_schema_forbids_unknown_citation_numbers():
    from radar.reports.pipeline import offer_contract

    contract = offer_contract({"sources": [{"n": 1}, {"n": 5}]})
    definitions = contract.model_json_schema()["$defs"]
    assert definitions["GroundedModule"]["properties"]["pain_refs"]["items"]["enum"] == [1, 5]


def test_report_progress_survives_reopen_and_repair_does_not_rewind(tmp_path):
    import shutil

    from radar.web.store import Store
    from radar.web.tasks import TaskRunner, report_progress

    shutil.copytree(ROOT / "config", tmp_path / "config")
    store = Store(tmp_path)
    tasks = TaskRunner(store, {"author": lambda *args: None})
    task = tasks.submit("author", "demo-1")
    assert tasks.latest_report("demo-2") is None
    report_progress(store, task["id"], 4, "Собираем PDF")
    report_progress(store, task["id"], 2, "Уточняем предложение", "Исправляем замечания")
    reopened = TaskRunner(Store(tmp_path), {"author": lambda *args: None})
    progress = reopened.latest_report("demo-1")["progress"]
    assert progress["completed"] == 4 and progress["total"] == 6
    assert progress["label"] == "Уточняем предложение"
    with store.connect() as db:
        db.execute("UPDATE object_tasks SET status='completed' WHERE id=?", (task["id"],))
    assert reopened.get(task["id"])["progress"]["completed"] == 6


@pytest.fixture(params=["nametkin-tower", "stantsiya-l"])
def v5_gold(request):
    return json.loads((ROOT / "src/radar/reports/gold" / (request.param + ".json")).read_text())


def test_v5_gold_contract_checks_and_complete_layout(v5_gold):
    from radar.reports.render import is_v5, page_count
    from radar.reports.schema_v5 import ReportV5
    from radar.reports.v5 import check

    value = ReportV5.model_validate(v5_gold).model_dump()
    assert is_v5(value) and check(value) == []
    html = build_html(value)
    assert html.count('class="page') == page_count(value) == 6
    assert "СЦЕНАРИИ И РИСКИ" in html.upper()
    assert "ПЕРВЫЕ" in html.upper() and "30 ДНЕЙ" in html.upper()
    assert "Как зайти к девелоперу" not in html


def test_v5_page_count_tracks_solutions_and_compact_layout(v5_gold):
    from radar.reports.render import page_count

    for modules, full, compact in [(2, 5, 4), (3, 6, 5), (4, 6, 5)]:
        value = json.loads(json.dumps(v5_gold))
        value["offer"]["modules"] = value["offer"]["modules"][:modules]
        assert page_count(value) == full
        assert build_html(value).count('class="page') == full
        value["meta"]["layout"] = "compact"
        html = build_html(value)
        assert page_count(value) == compact
        assert html.count('class="page') == compact
        assert 'data-page="diagnosis"' not in html


def test_v5_no_evidence_blocks_hide_without_fabrication(v5_gold):
    from radar.reports.schema_v5 import ReportV5

    v5_gold["situation"]["timeline"] = []
    v5_gold["situation"]["buyers"]["quotes"] = []
    v5_gold["situation"]["buyers"]["note"] = "Претензии касаются сроков и ответов компании."
    html = build_html(ReportV5.model_validate(v5_gold).model_dump())
    assert "Хронология" not in html
    assert "Претензии касаются сроков" in html
    assert "источник неясен" not in html


def test_v5_rejects_weak_serious_claim_and_changed_scenario(v5_gold):
    from radar.reports.v5 import check

    v5_gold["diagnosis"]["sources"][0]["type"] = "отзывы"
    weak = v5_gold["diagnosis"]["sources"][0]["n"]
    v5_gold["situation"]["facts"][0]["label"] = "Суд признал структуру застройщика банкротом"
    v5_gold["situation"]["facts"][0]["refs"] = [weak]
    assert any("серьёзное утверждение" in error for error in check(v5_gold))
    v5_gold["scenario"]["levels"][0]["exit"]["title"] = "Обещаем всё исправить"
    assert any("дерева отличается" in error for error in check(v5_gold))


def test_v5_scenario_is_deterministic_for_all_gold_stages(v5_gold):
    from radar.reports.scenario import build_scenario

    scenario = v5_gold["scenario"]
    assert (
        build_scenario(
            v5_gold["diagnosis"]["stage"]["current"],
            scenario["levels"][0]["risks"],
            {str(n): [r["title"] for r in scenario["levels"][n - 1]["risks"]] for n in (2, 3)},
            scenario["price"]["takeaway"],
            scenario["subtitle"],
        )
        == scenario
    )


def test_v5_monitoring_count_and_contract_not_people(v5_gold):
    from radar.reports.schema_v5 import ReportV5
    from radar.reports.v5 import check

    v5_gold["diagnosis"]["sources"][0]["count"] = 2
    with pytest.raises(ValidationError, match="greater than or equal to 5"):
        ReportV5.model_validate(v5_gold)
    v5_gold["diagnosis"]["sources"][0]["count"] = None
    v5_gold["situation"]["company"]["points"][0]["text"] = "Срок передачи по ДДУ — в договоре."
    assert not any("сокращение «ДДУ»" in error for error in check(v5_gold))
    v5_gold["situation"]["company"]["points"][0]["text"] = "Переговоры с ДДУ"
    assert any("сокращение «ДДУ»" in error for error in check(v5_gold))


def test_historical_report_keeps_legacy_template_and_page_count():
    from radar.reports.render import is_v5, page_count

    value = json.loads((ROOT / "src/radar/reports/gold/legacy/report-v1.json").read_text())
    assert not is_v5(value)
    assert page_count(value) == 4
    html = build_html(value)
    assert html.count('class="page') == 4
    assert "template/legacy/report.css" in html
    assert 'data-page="approach"' in html
    assert 'data-page="scenario"' not in html
