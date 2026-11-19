"""The model may propose one string, never a new citation or rewritten page."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from radar.reports import copy_edit
from radar.reports.factual_copy import validate_copy_numbers


def facts():
    return [
        {"text": "Ключи планировали в декабре 2027.", "date": "2026-01-30",
         "source_n": 1, "who_says": "СМИ", "kind": "claim",
         "verbatim": "Ключи планировали в декабре 2027."},
        {"text": "В корпусах 2 и 3 ведутся работы.", "date": "2026-10-28",
         "source_n": 2, "who_says": "Компания", "kind": "fact",
         "verbatim": "В корпусах 2 и 3 ведутся работы."},
    ]


def page():
    return {
        "lead": "Компания объявила новый ориентир.",
        "facts": [
            {"value": "2027", "label": "Новый срок ключей по корпусам 2 и 3",
             "refs": [1], "tone": "alert"},
        ],
        "buyers": {"status": "Есть обращения", "note": "У покупателей есть вопросы.",
                   "note_refs": [1], "quotes": [{"text": "Точная цитата", "refs": [1]}]},
        "timeline": [{"date": "2026", "date_iso": "2026", "text": "Срок изменили.",
                      "refs": [1]}],
    }


def test_request_exposes_only_exact_failed_string_and_its_cited_facts():
    previous = page()
    error = "situation.facts.0.label: Число в text отсутствует в своём источнике: 2; 3"
    value = copy_edit.request(error, facts(), previous)
    assert value["path"] == "situation.facts.0.label"
    assert value["current_text"] == previous["facts"][0]["label"]
    assert value["cited_refs"] == [1]
    assert {row["source_n"] for row in value["source_facts"]} == {1}
    assert "не расширяй refs" in value["instruction"]


def test_apply_changes_only_requested_leaf_and_deepcopies_all_other_fields():
    previous = page()
    before = deepcopy(previous)
    value = copy_edit.request("situation.facts.0.label: Число отсутствует: 2", facts(), previous)
    replacement = "Новый ориентир ключей по публикации"
    edited = copy_edit.apply(previous, value, replacement)
    expected = deepcopy(previous)
    expected["facts"][0]["label"] = replacement
    assert edited == expected and previous == before
    assert edited["facts"][0]["refs"] == previous["facts"][0]["refs"]
    assert edited["buyers"]["quotes"] == previous["buyers"]["quotes"]
    assert edited["timeline"] == previous["timeline"]
    edited["facts"][0]["refs"].append(2)
    assert previous["facts"][0]["refs"] == [1]


def test_assessment_path_resolves_without_rewriting_situation_or_refs():
    previous = {"diagnosis": {"main_problem": {"causes": [
        {"text": "До ключей осталось 14 месяцев.", "refs": [1]}
    ]}}}
    value = copy_edit.request(
        "diagnosis.main_problem.causes.0.text: Число отсутствует: 14", facts(), previous
    )
    assert value["cited_refs"] == [1]
    edited = copy_edit.apply(previous, value, "Объявлен новый ориентир ключей.")
    assert edited["diagnosis"]["main_problem"]["causes"][0] == {
        "text": "Объявлен новый ориентир ключей.", "refs": [1]
    }


def test_authority_claim_uses_only_module_citations_and_edits_only_addressed_leaves():
    page = {"modules": [{
        "pain": "Ведомство получило жалобу.", "pain_refs": [1],
        "outcome": "Справка будет готова.", "agency": ["Проверка уже началась."],
    }]}
    error = ValueError("Нужна проверка утверждений")
    error.issues = [
        {"path": "offer.modules.0.pain", "kind": "authority_claim", "reason": "Нужен источник"},
        {
            "path": "offer.modules.0.agency.0", "kind": "authority_claim",
            "reason": "Действие ведомства не подтверждено",
        },
    ]
    request = copy_edit.batch_request(error, facts(), page)
    assert [edit["cited_refs"] for edit in request["edits"]] == [[1], [1]]
    assert all(edit["source_facts"][0]["source_n"] == 1 for edit in request["edits"])
    patched = copy_edit.batch_apply(page, request, {"edits": [
        {"path": "offer.modules.0.pain", "text": "Дольщики ждут передачи ключей."},
        {"path": "offer.modules.0.agency.0", "text": "Подготовить фактическую справку."},
    ]})
    assert patched["modules"][0] == {
        "pain": "Дольщики ждут передачи ключей.", "pain_refs": [1],
        "outcome": "Справка будет готова.", "agency": ["Подготовить фактическую справку."],
    }
    assert page["modules"][0]["pain"] == "Ведомство получило жалобу."


@pytest.mark.parametrize("path", [
    "situation.buyers.quotes.0.text",
    "situation.timeline.0.date",
    "situation.timeline.0.date_iso",
    "situation.facts.0.refs",
    "situation.facts.0.value",
    "object.note",
    "passport.status",
    "identity.label",
    "meta.text",
    "situation.missing.0.text",
    "situation.facts.99.label",
])
def test_immutable_identity_quote_date_ref_and_absent_fields_are_not_editable(path):
    previous = {**page(), "object": {"note": "Название объекта"},
                "passport": {"status": "unknown"}, "identity": {"label": "Название"},
                "meta": {"text": "Версия"}}
    assert copy_edit.request(path + ": Число отсутствует: 2", facts(), previous) is None


@pytest.mark.parametrize("error", [
    "situation.facts.0.label: Слишком длинный текст",
    "situation.facts.0.label: Transport HTTP 429",
    "Число отсутствует: 2",
    "situation.facts.0.label",
])
def test_nonfactual_unaddressed_or_transport_errors_do_not_trigger_editor(error):
    assert copy_edit.request(error, facts(), page()) is None


@pytest.mark.parametrize("field", ["lead", "gap", "status", "note", "label", "text"])
def test_supported_leaf_names_can_be_targeted_after_specific_factual_error(field):
    previous = {"situation": {"block": {field: "Есть спорное число 14.", "refs": [1]}}}
    value = copy_edit.request(
        f"situation.block.{field}: Число отсутствует: 14", facts(), previous
    )
    assert value is not None and value["current_text"] == "Есть спорное число 14."


def test_apply_does_not_bypass_later_whole_page_validation():
    previous = page()
    value = copy_edit.request("situation.facts.0.label: Число отсутствует: 2", facts(), previous)
    edited = copy_edit.apply(previous, value, "Срок ключей по 999 корпусам.")
    with pytest.raises(ValueError):
        validate_copy_numbers(edited["facts"][0]["label"], facts(), [1])


def test_stale_request_and_tampered_immutable_path_fail_instead_of_overwriting():
    previous = page()
    value = copy_edit.request("situation.facts.0.label: Число отсутствует: 2", facts(), previous)
    previous["facts"][0]["label"] = "Уже исправлено другим проходом"
    with pytest.raises(ValueError, match="изменилась"):
        copy_edit.apply(previous, value, "Новая версия")
    value["path"] = "situation.buyers.quotes.0.text"
    with pytest.raises(ValueError, match="неизменяемо"):
        copy_edit.apply(previous, value, "Поддельная цитата")


def test_reply_allows_only_a_single_string_without_extra_reference_fields():
    assert copy_edit.Reply(text="Исправленная подпись").model_dump() == {
        "text": "Исправленная подпись"
    }
    with pytest.raises(ValidationError):
        copy_edit.Reply.model_validate({"text": "Подпись", "refs": [2]})
    with pytest.raises(ValidationError):
        copy_edit.Reply.model_validate({"text": {"nested": "page"}})


def test_exact_serious_claim_text_path_can_request_removal_of_unsupported_legal_detail():
    previous = {"diagnosis": {"stage": {"signs": [
        {"text": "Дольщики обратились в Следственный комитет.", "refs": [1]}
    ]}}}
    error = (
        "diagnosis.stage.signs.0.text: "
        "Серьёзное утверждение требует датированного СМИ или ведомства"
    )
    value = copy_edit.request(error, facts(), previous)
    assert value is not None and value["path"] == "diagnosis.stage.signs.0.text"
    assert value["cited_refs"] == [1]
    assert {row["source_n"] for row in value["source_facts"]} == {1}
    assert "юридическую конкретизацию" in copy_edit.PROMPT
    assert "с атрибуцией" in copy_edit.PROMPT
    assert "следств" in value["forbidden_legal_terms"]
    assert "убери все" in value["instruction"]


def test_serious_claim_error_still_cannot_rewrite_immutable_buyer_quote():
    error = (
        "situation.buyers.quotes.0.text: "
        "Серьёзное утверждение требует датированного СМИ или ведомства"
    )
    assert copy_edit.request(error, facts(), page()) is None


class SeveralIssues(ValueError):
    def __init__(self, issues):
        super().__init__("Страница содержит несколько фактических ошибок")
        self.issues = issues


def multi_request():
    previous = page()
    error = SeveralIssues([
        {"path": "situation.facts.0.label", "reason": "Число отсутствует: 2"},
        {"path": "situation.timeline.0.text",
         "reason": "Серьёзное утверждение требует датированного СМИ или ведомства"},
        {"path": "situation.facts.0.label", "reason": "Число отсутствует: 3"},
        {"path": "situation.buyers.quotes.0.text", "reason": "Число отсутствует: 2"},
        {"path": "situation.timeline.0.date_iso", "reason": "Дата не подтверждена"},
    ])
    return previous, copy_edit.batch_request(error, facts(), previous)


def test_batch_request_collects_each_editable_path_once_and_keeps_own_refs():
    previous, value = multi_request()
    assert [row["path"] for row in value["edits"]] == [
        "situation.facts.0.label", "situation.timeline.0.text"
    ]
    assert "2" in value["edits"][0]["error"] and "3" in value["edits"][0]["error"]
    assert all(row["cited_refs"] == [1] for row in value["edits"])
    assert all({fact["source_n"] for fact in row["source_facts"]} == {1}
               for row in value["edits"])
    assert previous == page()


def test_batch_contract_has_exact_count_and_only_the_allowed_path_literals():
    _, value = multi_request()
    schema = copy_edit.batch_contract(value).model_json_schema()
    array = schema["properties"]["edits"]
    assert array["minItems"] == array["maxItems"] == 2
    assert schema["$defs"]["CopyEdit"]["properties"]["path"]["enum"] == [
        "situation.facts.0.label", "situation.timeline.0.text"
    ]
    assert set(schema["$defs"]["CopyEdit"]["properties"]) == {"path", "text"}


def test_batch_apply_changes_only_both_requested_leaves_in_either_order():
    previous, value = multi_request()
    before = deepcopy(previous)
    edited = copy_edit.batch_apply(previous, value, {"edits": [
        {"path": "situation.timeline.0.text", "text": "Покупатели сообщили о претензиях."},
        {"path": "situation.facts.0.label", "text": "Новый ориентир ключей по публикации"},
    ]})
    expected = deepcopy(previous)
    expected["timeline"][0]["text"] = "Покупатели сообщили о претензиях."
    expected["facts"][0]["label"] = "Новый ориентир ключей по публикации"
    assert edited == expected and previous == before
    assert edited["buyers"]["quotes"] == before["buyers"]["quotes"]
    assert edited["timeline"][0]["date_iso"] == before["timeline"][0]["date_iso"]
    assert edited["facts"][0]["refs"] == before["facts"][0]["refs"]


@pytest.mark.parametrize("rows", [
    [{"path": "situation.facts.0.label", "text": "Один"}],
    [{"path": "situation.facts.0.label", "text": "Один"},
     {"path": "situation.facts.0.label", "text": "Повтор"}],
    [{"path": "situation.facts.0.label", "text": "Один"},
     {"path": "situation.buyers.quotes.0.text", "text": "Чужой путь"}],
    [{"path": "situation.facts.0.label", "text": "Один", "refs": [2]},
     {"path": "situation.timeline.0.text", "text": "Два"}],
])
def test_batch_reply_rejects_missing_duplicate_foreign_paths_and_extra_refs(rows):
    previous, value = multi_request()
    before = deepcopy(previous)
    with pytest.raises(ValueError):
        copy_edit.batch_apply(previous, value, {"edits": rows})
    assert previous == before


def test_single_error_is_adapted_to_batch_and_no_editable_issues_returns_none():
    previous = page()
    single = copy_edit.batch_request(
        "situation.facts.0.label: Число отсутствует: 2", facts(), previous
    )
    assert len(single["edits"]) == 1
    assert copy_edit.batch_request(SeveralIssues([
        {"path": "situation.buyers.quotes.0.text", "reason": "Число отсутствует: 2"}
    ]), facts(), previous) is None
    assert copy_edit.batch_request(SeveralIssues([]), facts(), previous) is None


def test_batch_apply_still_requires_later_strict_page_validation():
    previous, value = multi_request()
    edited = copy_edit.batch_apply(previous, value, {"edits": [
        {"path": row["path"], "text": "Корпус 999"}
        for row in value["edits"]
    ]})
    with pytest.raises(ValueError):
        validate_copy_numbers(edited["facts"][0]["label"], facts(), [1])


def legal_request():
    previous = {"diagnosis": {"stage": {"signs": [
        {"text": "Дольщики обратились в Следственный комитет.", "refs": [1]}
    ]}}}
    return copy_edit.request(
        "diagnosis.stage.signs.0.text: "
        "Серьёзное утверждение требует датированного СМИ или ведомства",
        facts(), previous
    )


def test_single_contract_rejects_investigation_synonym_with_precise_feedback():
    value = legal_request()
    with pytest.raises(ValidationError, match="следств") as caught:
        copy_edit.single_contract(value).model_validate({
            "text": "Дольщики обратились в надзорные и следственные органы."
        })
    assert value["path"] in str(caught.value)
    assert "юридическая конкретизация осталась" in str(caught.value)


@pytest.mark.parametrize("text", [
    "По сообщениям покупателей, они направили жалобу в надзор.",
    "Впоследствии покупатели сообщили о претензиях к срокам.",
    "Дольщики обсуждают последствия переноса сроков.",
])
def test_single_contract_keeps_general_attributed_complaint_and_ordinary_chronology(text):
    assert copy_edit.single_contract(legal_request()).model_validate({"text": text}).text == text


def test_nonnumeric_legal_constraints_are_only_attached_to_serious_error_request():
    value = copy_edit.request("situation.facts.0.label: Число отсутствует: 2", facts(), page())
    assert "forbidden_legal_terms" not in value
    assert copy_edit.single_contract(value).model_validate({"text": "В суде"}).text == "В суде"


def test_batch_contract_checks_legal_terms_per_path_and_keeps_exact_path_set():
    previous, value = multi_request()
    legal_path = "situation.timeline.0.text"
    with pytest.raises(ValidationError, match="следств"):
        copy_edit.batch_contract(value).model_validate({"edits": [
            {"path": "situation.facts.0.label", "text": "Ориентир ключей"},
            {"path": legal_path, "text": "Покупатели обратились в следственные органы."},
        ]})
    good = {"edits": [
        {"path": "situation.facts.0.label", "text": "Ориентир ключей"},
        {"path": legal_path, "text": "Покупатели сообщили о жалобе в надзор."},
    ]}
    assert copy_edit.batch_apply(previous, value, good)["timeline"][0]["text"] == good["edits"][1][
        "text"
    ]
    with pytest.raises(ValidationError, match="ровно один раз"):
        copy_edit.batch_contract(value).model_validate({"edits": [good["edits"][0]] * 2})


def test_duplicate_numeric_and_serious_issue_same_path_retains_legal_constraint():
    value = copy_edit.batch_request(SeveralIssues([
        {"path": "situation.facts.0.label", "reason": "Число отсутствует: 2"},
        {"path": "situation.facts.0.label",
         "reason": "Серьёзное утверждение требует датированного СМИ или ведомства"},
    ]), facts(), page())
    assert len(value["edits"]) == 1
    assert "следств" in value["edits"][0]["forbidden_legal_terms"]
    with pytest.raises(ValidationError):
        copy_edit.batch_contract(value).model_validate({"edits": [{
            "path": "situation.facts.0.label", "text": "Следственные органы"
        }]})
