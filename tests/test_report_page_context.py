import json
from types import SimpleNamespace

import pytest

from radar.reports.pipeline_v8 import page_context, previous_page


@pytest.mark.parametrize("page", ["situation", "assessment", "scenario"])
def test_factual_pages_retain_all_evidence_without_offer_catalogue_noise(page):
    # Longer than the former prose limits: page selection must never shorten evidence.
    fact_text = "Проверенное описание сроков с сохранением всех важных оговорок. " * 20
    source_text = "Полный прочитанный текст первоисточника. " * 500
    quote_text = "Обещанный срок передачи прошёл, требуется содержательный ответ. " * 10
    context = {
        "object": {"name": "ЖК Тест"},
        "facts": {"facts": [{"source_n": 1, "text": fact_text}]},
        "sources": [{"n": 1, "url": "https://example.org/news", "text": source_text}],
        "buyer_quotes": [{"text": quote_text, "refs": [1]}],
        "qualification": {"current_stage": 3},
        "focus": {"primary_problem": {"id": "deadline"}},
        "report_scope": {"phase": "III очередь"},
        "pdf_registry_mapping": {"7": 1},
        "monitoring_scale": {"max": 10},
        "current_monitoring_score": 6,
        "allowed_risks": {"2": ["Повторные обращения"]},
        "matrix_cards": [{"solution": "ARCHIVE_OFFER_CARD"}],
        "methods": [{"name": "ARCHIVE_METHOD"}],
        "services": [{"name": "ARCHIVE_SERVICE"}],
    }
    selected = page_context(context, page)
    assert selected["facts"] == context["facts"]
    assert selected["sources"] == context["sources"]
    assert selected["qualification"] == context["qualification"]
    assert selected["focus"] == context["focus"]
    assert selected["report_scope"] == context["report_scope"]
    assert selected["pdf_registry_mapping"] == context["pdf_registry_mapping"]
    assert "ARCHIVE_" not in json.dumps(selected)
    serialized = json.dumps(selected, ensure_ascii=False)
    assert fact_text in serialized and source_text in serialized
    if page == "situation":
        assert selected["buyer_quotes"] == context["buyer_quotes"]
        assert quote_text in serialized
    if page == "assessment":
        assert selected["current_monitoring_score"] == 6
        assert selected["monitoring_scale"] == {"max": 10}
    if page == "scenario":
        assert selected["allowed_risks"] == context["allowed_risks"]
    # Selection is read-only: the later offer still sees the original methods/cards.
    assert page_context(context, "offer") == context


def test_rejected_candidate_is_kept_for_next_edit_without_mutating_it():
    draft = {"buyers": {"quotes": [{"quote_key": "q1"}]}, "lead": "Непроверенный текст"}
    runner = SimpleNamespace(candidates={"situation": draft})
    selected = previous_page({}, runner, "situation", [{"quote_key": "q1"}])
    assert selected == draft
    selected["lead"] = "Исправленный текст"
    assert draft["lead"] == "Непроверенный текст"


def test_serious_copy_is_checked_on_situation_before_dependent_assessment():
    from radar.reports.pipeline_v8 import ResearchFacts, validate_serious_copy

    sources = [{"n": 1, "contour": "reviews"}, {"n": 2, "contour": "media"}]
    statement = "Следственный комитет начал проверку"
    facts = ResearchFacts(facts=[{
        "source_n": 2, "date": "2026-11-01", "text": statement, "verbatim": statement,
        "who_says": "СМИ", "kind": "fact",
    }])
    block = {"situation": {"buyers": {"topics": [
        {"text": statement, "refs": [1]}
    ]}}}
    with pytest.raises(ValueError, match=r"^situation.buyers.topics.0.text: Серьёзное"):
        validate_serious_copy(block, facts, sources)
    block["situation"]["buyers"]["topics"][0]["refs"] = [2]
    validate_serious_copy(block, facts, sources)


def test_named_forum_can_report_a_filing_but_cannot_establish_an_investigation():
    from radar.reports.pipeline_v8 import ResearchFacts, validate_serious_copy

    sources = [{"n": 1, "contour": "reviews", "url": "https://forum.example/topic/1"}]
    quote = "Собственники коллективно обратились в Следственный комитет."
    facts = ResearchFacts(facts=[{
        "source_n": 1, "date": "2026-02-01", "text": quote, "verbatim": quote,
        "who_says": "Собственники", "kind": "claim",
    }])
    statement = "На форуме forum.example сообщили о коллективном обращении в Следственный комитет."
    assert not validate_serious_copy({"situation": {"timeline": [
        {"text": statement, "refs": [1]},
    ]}}, facts, sources)
    with pytest.raises(ValueError, match="действии ведомства|Серьёзное утверждение"):
        validate_serious_copy({"situation": {"timeline": [
            {"text": statement + " Следственный комитет начал проверку.", "refs": [1]},
        ]}}, facts, sources)


def test_accepted_page_is_preferred_and_quotes_return_to_immutable_keys():
    accepted = {"buyers": {"quotes": [{"text": "Жду ответ", "refs": [2]}]}}
    runner = SimpleNamespace(candidates={"situation": {"lead": "Old rejected draft"}})
    options = [{"quote_key": "q1", "text": "Жду ответ", "refs": [2]}]
    assert previous_page({"situation": accepted}, runner, "situation", options) == {
        "buyers": {"quotes": [{"quote_key": "q1"}]}
    }
    assert accepted["buyers"]["quotes"] == [{"text": "Жду ответ", "refs": [2]}]


def test_failed_revision_uses_its_own_candidate_instead_of_older_accepted_refs():
    pages = {"situation": {"buyers": {"quotes": []}, "facts": [{"refs": [1]}]}}
    candidate = {"buyers": {"quotes": []}, "facts": [{"refs": [2]}]}
    runner = SimpleNamespace(candidates={"situation": candidate})
    selected = previous_page(pages, runner, "situation", [], prefer_candidate=True)
    assert selected == candidate and selected["facts"][0]["refs"] == [2]
    assert pages["situation"]["facts"][0]["refs"] == [1]


def test_runner_retains_parsed_draft_even_when_page_validator_rejects_it(monkeypatch, tmp_path):
    import test_report_pipeline_v5 as fixtures

    from radar.reports import pipeline_v8 as pipeline

    monkeypatch.setattr(fixtures, "pipeline", pipeline)
    store, _ = fixtures.harness.__wrapped__(monkeypatch, tmp_path)
    calls = []

    def invoke(*args):
        calls.append(json.loads(args[3]["messages"][1]["content"]))
        return fixtures.response({"facts": []}, tool=True)

    def reject(value):
        # Mutating validation must not change the candidate's saved draft.
        value.facts.append("rejected mutation")
        raise ValueError("situation.timeline.0: unsupported date")

    runner = pipeline.Runner(store, "job", object(), invoke)
    with pytest.raises(pipeline.NeedsReview, match="unsupported date"):
        runner.step("situation", pipeline.FactsBatch, "test", {}, "author", reject)
    assert runner.candidates["situation"] == {"facts": []}
    assert len(calls) == 2
    assert json.loads(calls[1]["previous"]) == {"facts": []}


@pytest.mark.parametrize("valid_edit", [True, False])
@pytest.mark.parametrize("error_kind", ["number", "serious"])
def test_assessment_retry_gets_precise_field_refs_and_prohibits_unproved_arithmetic(
    monkeypatch, tmp_path, valid_edit, error_kind
):
    import test_report_pipeline_v5 as fixtures

    from radar.reports import pipeline_v8 as pipeline
    from radar.reports.schema import Model

    class Draft(Model):
        diagnosis: dict

    monkeypatch.setattr(fixtures, "pipeline", pipeline)
    store, _ = fixtures.harness.__wrapped__(monkeypatch, tmp_path)
    requests = []
    original = "Через 14 месяцев" if error_kind == "number" else (
        "Следственный комитет начал расследование"
    )

    def invoke(*args):
        requests.append(json.loads(args[3]["messages"][1]["content"]))
        if args[4] == "Отчёт: copy_edit:assessment":
            return fixtures.response({"text": (
                "В октябре выполнялись работы" if valid_edit else original
            )}, tool=True)
        return fixtures.response({"diagnosis": {"main_problem": {"causes": [
            {"text": original, "refs": [1]}
        ]}}}, tool=True)

    def validate(value):
        point = value.diagnosis["main_problem"]["causes"][0]
        if "Следственный" in point["text"]:
            raise ValueError(
                "diagnosis.main_problem.causes.0.text: Серьёзное утверждение требует "
                "датированного СМИ или ведомства"
            )
        pipeline.factual_copy.validate_copy_numbers(
            point["text"], facts, point["refs"], path="diagnosis.main_problem.causes.0.text"
        )
        return value

    facts = {"facts": [
        {"source_n": 1, "text": "В октябре выполнялись работы", "date": "2026-10-28",
         "verbatim": "В октябре выполнялись работы"},
        {"source_n": 2, "text": "Участник назвал 14,5 млрд рублей", "date": None,
         "verbatim": "Участник назвал 14,5 млрд рублей"},
    ]}
    runner = pipeline.Runner(store, "job", object(), invoke)
    if valid_edit:
        value = runner.step("assessment", Draft, "test", {"facts": facts}, "author", validate)
        assert "14" not in value.diagnosis["main_problem"]["causes"][0]["text"]
        assert value.diagnosis["main_problem"]["causes"][0]["refs"] == [1]
    else:
        with pytest.raises(ValueError, match="Число|Серьёзное|юридическая конкретизация"):
            runner.step("assessment", Draft, "test", {"facts": facts}, "author", validate)
    assert len(requests) == (3 if error_kind == "serious" and not valid_edit else 2)
    assert requests[1]["data"]["path"] == "diagnosis.main_problem.causes.0.text"
    assert requests[1]["data"]["current_text"] == original
    assert requests[1]["data"]["cited_refs"] == [1]
    assert {r["source_n"] for r in requests[1]["data"]["source_facts"]} == {1}


@pytest.mark.parametrize("aggregate", [True, False])
def test_multiple_field_errors_are_edited_without_regenerating_whole_page(
    monkeypatch, tmp_path, aggregate
):
    import test_report_pipeline_v5 as fixtures

    from radar.reports import pipeline_v8 as pipeline
    from radar.reports.schema import Model

    class Draft(Model):
        diagnosis: dict

    monkeypatch.setattr(fixtures, "pipeline", pipeline)
    store, _ = fixtures.harness.__wrapped__(monkeypatch, tmp_path)
    calls = []

    def invoke(*args):
        label = args[4]
        calls.append(label)
        data = json.loads(args[3]["messages"][1]["content"])["data"]
        if label == "Отчёт: assessment":
            value = {"diagnosis": {"causes": [
                {"text": "Через 14 месяцев", "refs": [1]},
                {"text": "Следственный комитет начал проверку", "refs": [1]},
            ]}}
        elif "edits" in data:
            value = {"edits": [{"path": row["path"], "text": "Выполнялись работы"}
                               for row in data["edits"]]}
        else:
            value = {"text": "Выполнялись работы"}
        return fixtures.response(value, tool=True)

    def validate(value):
        issues = []
        for i, point in enumerate(value.diagnosis["causes"]):
            path = f"diagnosis.causes.{i}.text"
            if "14" in point["text"]:
                issues.append({"path": path, "reason": "Число не подтверждено: 14"})
            if "Следственный" in point["text"]:
                issues.append({"path": path, "reason": (
                    "Серьёзное утверждение требует датированного СМИ или ведомства"
                )})
        if issues:
            if aggregate:
                raise pipeline.CopyValidationError(issues)
            raise ValueError(issues[0]["path"] + ": " + issues[0]["reason"])
        return value

    facts = {"facts": [{"source_n": 1, "text": "Выполнялись работы", "date": None,
                        "verbatim": "Выполнялись работы"}]}
    runner = pipeline.Runner(store, "job", object(), invoke)
    result = runner.step("assessment", Draft, "test", {"facts": facts}, "author", validate)
    assert [r["text"] for r in result.diagnosis["causes"]] == ["Выполнялись работы"] * 2
    assert all(r["refs"] == [1] for r in result.diagnosis["causes"])
    assert calls.count("Отчёт: assessment") == 1
    assert calls.count("Отчёт: copy_edit:assessment") == (1 if aggregate else 2)
    expected = 1 if aggregate else 2
    assert runner.repairs_by_stage == {"copy": expected, "copy:assessment": expected}
    assert runner.repairs == 0 and runner.copy_repairs == (1 if aggregate else 2)


@pytest.mark.parametrize("copy_first", [True, False])
def test_eight_copy_batches_are_independent_of_six_full_page_or_schema_repairs(
    monkeypatch, tmp_path, copy_first
):
    import test_report_pipeline_v5 as fixtures

    from radar.reports import pipeline_v8 as pipeline

    monkeypatch.setattr(fixtures, "pipeline", pipeline)
    store, _ = fixtures.harness.__wrapped__(monkeypatch, tmp_path)
    runner = pipeline.Runner(store, "job", object())
    stages = ["editorial", "editorial", "critic", "critic", "assessment", "situation"]

    def copy_budget():
        for expected in range(1, 9):
            runner.reserve_copy_repair("situation")
            assert runner.copy_repairs == expected
        with pytest.raises(pipeline.NeedsReview, match="Точечные"):
            runner.reserve_copy_repair("situation")
        assert runner.copy_repairs == 8 and runner.repairs_by_stage["copy"] == 8

    def full_budget():
        for expected, stage in enumerate(stages, 1):
            runner.repair(stage)
            assert runner.repairs == expected
        with pytest.raises(pipeline.NeedsReview, match="исчерпаны"):
            runner.repair("scenario")
        assert runner.repairs == 6

    if copy_first:
        copy_budget()
        assert runner.repairs == 0
        full_budget()
    else:
        full_budget()
        assert runner.copy_repairs == 0
        copy_budget()
    assert runner.repairs == 6 and runner.copy_repairs == 8
    for page, limit in (("assessment", 3), ("scenario", 2), ("offer", 3)):
        for _ in range(limit):
            runner.reserve_copy_repair(page)
        with pytest.raises(pipeline.NeedsReview, match="Точечные"):
            runner.reserve_copy_repair(page)
    assert runner.copy_repairs == 16


def test_saved_audit_reports_total_full_page_and_copy_repairs_separately(monkeypatch, tmp_path):
    import test_report_pipeline_v5 as fixtures

    from radar.reports import pipeline_v8 as pipeline

    monkeypatch.setattr(fixtures, "pipeline", pipeline)
    store, payload = fixtures.harness.__wrapped__(monkeypatch, tmp_path)
    runner = pipeline.Runner(store, "job", object())
    runner.reserve_copy_repair("situation")
    runner.reserve_copy_repair("assessment")
    runner.repair("assessment")
    saved = pipeline._save(
        store, "record", payload, runner, "fingerprint", {"verdict": "hold"},
        None, [], [],
    )
    audit = saved["payload"]["report_pipeline"]
    assert audit["repairs"] == 3
    assert audit["full_page_repairs"] == 1
    assert audit["copy_repairs"] == 2
    assert audit["repairs_by_stage"] == {
        "copy": 2, "copy:situation": 1, "copy:assessment": 1, "assessment": 1,
    }

