"""A model's editorial verdict is evidence to validate, not permission to rewrite."""

import copy

import pytest
from pydantic import ValidationError

from radar.reports import prompts_v8 as prompts
from radar.reports import review_v8 as review
from radar.reports.scenario_v8 import PLAYBOOK, build_scenario, stages_block


@pytest.fixture
def report():
    return {
        "meta": {"date": "17.11.2026"},
        "object": {"name": "Nagatino I-Land"},
        "situation": {
            "lead": "СМИ сообщили о переносе ключей с марта 2026 на декабрь 2027 года.",
            "company": {"points": [{"text": "В феврале компания объяснила геологические работы."}]},
        },
        "diagnosis": {"sources": [{"n": 1}, {"n": 2}]},
        "offer": {
            "usp": "Nagatino I-Land: ведём переговоры по последствиям переноса ключей.",
            "why_now": ["СМИ назвали декабрь 2027 года новым ориентиром ключей."],
            "modules": [
                {
                    "name": "Позиция о сроках и этапах",
                    "service": "Коммуникации с дольщиками",
                    "service_ids": ["S02"],
                    "method_ids": ["M01"],
                    "move": "A01",
                    "pain": "Ввод и ключи названы разными сроками.",
                    "pain_refs": [1],
                    "solution": "Готовим единое сообщение о сроках по каждому корпусу.",
                    "agency": ["Организуем съёмку и готовим спикера к сложным вопросам."],
                    "developer": ["Подтверждает технические факты и согласует позицию."],
                    "outcome": "Снизить повторные обращения о сроках.",
                }
            ],
        },
    }


def checks(criterion=None, *, quote="", path=None, passed=False):
    result = {
        "checks": [
            {
                "criterion": number,
                "passed": True,
                "quote": "",
                "reason": "Проверены соответствующие поля и доказательства отчёта.",
            }
            for number in range(1, 10)
        ]
    }
    if criterion:
        result["checks"][criterion - 1].update(
            passed=passed, quote=quote, path=path, reason="Уточни конкретное поле модуля."
        )
    return result


def test_failed_report_quote_is_anchored_and_only_its_page_is_repaired(report):
    critique = checks(4, quote="Готовим единое сообщение", path="/offer/modules/0/solution")
    value = review.validate_critique(critique, report)
    assert not value.passed and value.checks[3].passed is False
    feedback = review.repair_feedback(value, report)
    assert feedback["pages"] == ["offer"]
    assert feedback["issues"] == [
        {
            "criterion": 4,
            "page": "offer",
            "path": "/offer/modules/0/solution",
            "quote": "Готовим единое сообщение",
            "reason": "Уточни конкретное поле модуля.",
        }
    ]


def test_legacy_unique_quote_resolves_without_mutating_input(report):
    critique = checks(2, quote="В феврале компания объяснила")
    before = copy.deepcopy(critique)
    value = review.validate_critique(critique, report)
    assert value.checks[1].path == "/situation/company/points/0/text"
    assert critique == before
    assert review.repair_feedback(value, report)["pages"] == ["situation"]


def test_repeated_quote_requires_an_explicit_field(report):
    report["offer"]["modules"][0]["agency"].append("Готовим единое сообщение о сроках.")
    with pytest.raises(review.ReviewGroundingError) as failure:
        review.validate_critique(checks(4, quote="Готовим единое сообщение"), report)
    assert len(failure.value.feedback["issues"][0]["candidate_paths"]) == 2
    valid = checks(4, quote="Готовим единое сообщение", path="/offer/modules/0/solution")
    assert not review.validate_critique(valid, report).passed


@pytest.mark.parametrize("passed", [False, True])
def test_live_unused_catalogue_quote_never_becomes_a_report_verdict(report, passed):
    # The live Nagatino critic quoted S04, although that service was not in Offer.
    quote = (
        "Мониторинг открытых Telegram, MAX и VK сообществ; сигналы, ежедневные и недельные "
        "сводки, карточки событий."
    )
    with pytest.raises(review.ReviewGroundingError) as failure:
        review.validate_critique(checks(3, quote=quote, passed=passed), report)
    assert failure.value.feedback["kind"] == "critic_grounding_error"
    assert failure.value.feedback["issues"][0]["candidate_paths"] == []
    assert "неизменённый report" in failure.value.feedback["instruction"]


@pytest.mark.parametrize(
    "path",
    [
        "/facts/facts/0/text",
        "/offer/modules/0/pain",  # The same words do not occur in this field.
        "/offer/modules/0",
        "offer/modules/0/solution",
        "/offer/modules/0/solu~2tion",
        "/offer/modules/0/../solution",
    ],
)
def test_wrong_or_non_string_paths_cannot_anchor_real_words_elsewhere(report, path):
    with pytest.raises(review.ReviewGroundingError):
        review.validate_critique(checks(4, quote="Готовим единое сообщение", path=path), report)


def test_quote_must_be_one_field_not_a_json_fragment_or_join_of_fields(report):
    with pytest.raises(review.ReviewGroundingError):
        review.validate_critique(checks(2, quote='"pain_refs": [1]'), report)
    with pytest.raises(review.ReviewGroundingError):
        review.validate_critique(checks(8, quote="март 2026 / декабрь 2027 / февраль"), report)


def test_factual_why_now_is_not_a_standalone_sales_or_roles_block(report):
    quote = report["offer"]["why_now"][0]
    for criterion in (4, 5, 9):
        with pytest.raises(review.ReviewGroundingError):
            review.validate_critique(
                checks(criterion, quote=quote, path="/offer/why_now/0"), report
            )
    # A real factual/date error in that same field still belongs to criterion 2.
    value = review.validate_critique(checks(2, quote=quote, path="/offer/why_now/0"), report)
    assert not value.passed


def test_whole_module_roles_and_measurable_goals_remain_reviewable(report):
    module = report["offer"]["modules"][0]
    role = checks(9, quote=module["developer"][0], path="/offer/modules/0/developer/0")
    goal = checks(7, quote=module["outcome"], path="/offer/modules/0/outcome", passed=True)
    assert not review.validate_critique(role, report).passed
    assert review.validate_critique(goal, report).passed


def test_all_nine_independent_checks_and_real_reasons_are_required(report):
    for number in (8, 10):
        invalid = checks()
        invalid["checks"] = invalid["checks"][:number]
        if number == 10:
            invalid["checks"].append(invalid["checks"][0])
        with pytest.raises(ValidationError):
            review.validate_critique(invalid, report)
    duplicate = checks()
    duplicate["checks"][-1]["criterion"] = 8
    with pytest.raises(ValidationError):
        review.validate_critique(duplicate, report)
    blank = checks()
    blank["checks"][0]["reason"] = " "
    with pytest.raises(review.ReviewGroundingError):
        review.validate_critique(blank, report)
    with pytest.raises(review.ReviewGroundingError):
        review.validate_critique(checks(2), report)


def native_checks(options, *, criterion=None, path=None, passed=False):
    value = {"checks": [
        {"criterion": number, "passed": True, "field_key": None,
         "reason": "Проверены соответствующие поля и доказательства отчёта."}
        for number in range(1, 10)
    ]}
    if criterion is not None:
        key = next(row["field_key"] for row in options if row["path"] == path)
        value["checks"][criterion - 1].update(
            field_key=key, passed=passed, reason="Уточните выбранное поле отчёта."
        )
    return value


def test_native_options_are_exact_visible_fields_with_stable_keys_and_json_pointers(report):
    report["situation"]["lead"] += " Очень длинное подтверждённое поле." * 20
    options = review.field_options(report)
    assert [row["field_key"] for row in options] == [
        f"r{n:04d}" for n in range(1, len(options) + 1)
    ]
    assert [row["path"] for row in options] == sorted(row["path"] for row in options)
    paths = {row["path"] for row in options}
    assert "/situation/lead" in paths and "/offer/modules/0/solution" in paths
    assert not any("/meta/" in path or "/service_ids/" in path or "/method_ids/" in path
                   or path.endswith(("/move", "/url")) for path in paths)
    assert next(row["text"] for row in options if row["path"] == "/situation/lead") == report[
        "situation"
    ]["lead"]
    pain = next(row for row in options if row["path"] == "/offer/modules/0/pain")
    solution = next(row for row in options if row["path"] == "/offer/modules/0/solution")
    assert 4 not in pain["allowed_criteria"] and 4 in solution["allowed_criteria"]
    assert all(row["allowed_criteria"] == [
        n for n in range(1, 10) if review._scope(n, row["path"])
    ] for row in options)
    reordered = {key: report[key] for key in reversed(report)}
    assert review.field_options(reordered) == options


def test_configured_text_is_not_selectable_but_authored_stage_and_scenario_are(report):
    report["diagnosis"]["stage"] = {
        "stages": stages_block(),
        "confidence": "В форуме обсуждались сроки.",
        "signs": [{"text": "Сообщалось о коллективном обращении."}],
        "forecast": "Вероятны новые обращения.",
    }
    report["scenario"] = build_scenario(
        2,
        [{"title": "Перенос сроков", "text": "Сообщалось о переносе."}] * 3,
        {i: [row["title"] for row in PLAYBOOK["risk_pools"][f"stage_{i + 1}"]][:3]
         for i in (2, 3)},
        "Согласуем позицию по переносу.",
        "Перенос сроков и претензии",
    )
    report["offer"]["subtitle"] = PLAYBOOK["offer_subtitles"][2]
    options = review.field_options(report)
    paths = {row["path"] for row in options}
    assert {
        "/diagnosis/stage/confidence", "/diagnosis/stage/signs/0/text", "/diagnosis/stage/forecast",
    } <= paths
    assert not any(path.startswith("/diagnosis/stage/stages/") for path in paths)
    assert "/offer/subtitle" not in paths
    assert {path for path in paths if path.startswith("/scenario/")} == {
        "/scenario/subtitle", "/scenario/price/takeaway",
        *(f"/scenario/levels/{level}/risks/{i}/{key}"
          for level in range(3) for i in range(3) for key in ("title", "text")),
    }
    for path, page in (("/diagnosis/stage/confidence", "assessment"),
                       ("/scenario/levels/0/risks/0/text", "scenario"),
                       ("/scenario/levels/1/risks/2/title", "scenario")):
        draft = native_checks(options, criterion=2, path=path)
        value = review.resolve_native(draft, options, report)
        assert review.repair_feedback(value, report)["pages"] == [page]


def test_legacy_critic_cannot_send_stage_definition_for_rewriting(report):
    stages = stages_block()
    report["diagnosis"]["stage"] = {"stages": stages}
    path = "/diagnosis/stage/stages/0/desc"
    quote = stages[0]["desc"]
    with pytest.raises(review.ReviewGroundingError, match="не может быть исправлено"):
        review.repair_feedback(checks(6, path=path, quote=quote), report)
    with pytest.raises(review.ReviewGroundingError):
        review.repair_feedback(checks(6, quote=quote), report)
    # Old approval records with explicit quotes are still readable.
    approval = review.validate_critique(checks(6, path=path, quote=quote, passed=True), report)
    assert approval.passed and review.repair_feedback(approval, report)["issues"] == []


def test_native_contract_contains_only_key_not_free_quote_or_path(report):
    options = review.field_options(report)
    schema = review.native_contract(options).model_json_schema()
    props = schema["$defs"]["NativeReviewCheck"]["properties"]
    assert set(props) == {"criterion", "passed", "field_key", "reason"}
    assert props["field_key"]["anyOf"][0]["enum"] == [row["field_key"] for row in options]
    assert schema["properties"]["checks"]["minItems"] == 9
    assert schema["properties"]["checks"]["maxItems"] == 9


def test_native_resolution_inserts_entire_original_field_and_preserves_failed_verdict(report):
    options = review.field_options(report)
    path = "/offer/modules/0/solution"
    draft = native_checks(options, criterion=4, path=path)
    before = copy.deepcopy(draft)
    result = review.resolve_native(draft, options, report)
    assert not result.passed and result.checks[3].path == path
    assert result.checks[3].quote == report["offer"]["modules"][0]["solution"]
    assert draft == before
    assert review.repair_feedback(result, report)["pages"] == ["offer"]


def test_native_exact_quote_is_the_entire_long_field_without_legacy_length_or_clipping(report):
    report["situation"]["lead"] = "Проверенное описание ситуации с важной оговоркой. " * 20
    options = review.field_options(report)
    value = review.resolve_native(
        native_checks(options, criterion=2, path="/situation/lead"), options, report
    )
    assert len(value.checks[1].quote) > 220
    assert value.checks[1].quote == report["situation"]["lead"]


def test_native_approvals_can_use_null_or_a_valid_scoped_field(report):
    options = review.field_options(report)
    all_passed = review.resolve_native(native_checks(options), options, report)
    assert all_passed.passed and all(c.quote == "" and c.path is None for c in all_passed.checks)
    result = review.resolve_native(
        native_checks(options, criterion=7, path="/offer/modules/0/outcome", passed=True),
        options, report,
    )
    assert result.passed and result.checks[6].quote == report["offer"]["modules"][0]["outcome"]


def test_native_failed_null_unknown_key_and_free_quote_or_path_are_rejected(report):
    options = review.field_options(report)
    failed = native_checks(options)
    failed["checks"][0]["passed"] = False
    with pytest.raises(ValidationError, match="field_key"):
        review.resolve_native(failed, options, report)
    for foreign in ["r9999", "/facts/facts/13", "/price/takeaway"]:
        invalid = native_checks(options)
        invalid["checks"][0]["field_key"] = foreign
        with pytest.raises(ValidationError):
            review.resolve_native(invalid, options, report)
    for field in ["quote", "path"]:
        invalid = native_checks(options)
        invalid["checks"][0][field] = "Copied from outside report"
        with pytest.raises(ValidationError):
            review.resolve_native(invalid, options, report)


def test_native_field_key_does_not_weaken_criterion_scope(report):
    options = review.field_options(report)
    draft = native_checks(options, criterion=4, path="/offer/why_now/0")
    with pytest.raises(review.ReviewGroundingError, match="предмету"):
        review.resolve_native(draft, options, report)
    assert not review.resolve_native(
        native_checks(options, criterion=2, path="/offer/why_now/0"), options, report
    ).passed


def test_native_approval_omits_optional_quote_outside_criterion_scope(report):
    report["offer"]["pilot"] = {"columns": [{"points": ["Собираем повторяющиеся вопросы."]}]}
    options = review.field_options(report)
    path = "/offer/pilot/columns/0/points/0"
    draft = native_checks(options, criterion=3, path=path, passed=True)
    result = review.resolve_native(draft, options, report)
    check = result.checks[2]
    assert result.passed and check.path is None and not check.quote
    assert check.reason == draft["checks"][2]["reason"]
    draft["checks"][2]["passed"] = False
    with pytest.raises(review.ReviewGroundingError, match="предмету"):
        review.resolve_native(draft, options, report)


def test_native_duplicate_or_missing_criteria_still_fail_existing_nine_check_contract(report):
    options = review.field_options(report)
    duplicate = native_checks(options)
    duplicate["checks"][-1]["criterion"] = 8
    with pytest.raises(ValidationError, match="девять"):
        review.resolve_native(duplicate, options, report)
    incomplete = native_checks(options)
    incomplete["checks"].pop()
    with pytest.raises(ValidationError):
        review.resolve_native(incomplete, options, report)


def test_native_snapshot_cannot_anchor_changed_field_or_forged_option(report):
    options = review.field_options(report)
    draft = native_checks(options, criterion=2, path="/situation/lead")
    changed = copy.deepcopy(report)
    changed["situation"]["lead"] += " Добавилось новое утверждение."
    with pytest.raises(review.ReviewGroundingError, match="текущему"):
        review.resolve_native(draft, options, changed)
    forged = copy.deepcopy(options)
    forged[0]["text"] = "Текст из facts"
    with pytest.raises(review.ReviewGroundingError):
        review.resolve_native(draft, forged, report)


def test_native_duplicate_text_is_disambiguated_by_field_key_without_fuzzy_matching(report):
    report["offer"]["modules"][0]["agency"].append(report["offer"]["modules"][0]["solution"])
    options = review.field_options(report)
    path = "/offer/modules/0/agency/1"
    value = review.resolve_native(native_checks(options, criterion=4, path=path), options, report)
    assert value.checks[3].path == path
    assert value.checks[3].quote == report["offer"]["modules"][0]["solution"]


def test_compact_context_preserves_contrary_facts_but_not_unused_catalogue_people(report):
    context = {
        "facts": {
            "facts": [
                {
                    "text": "СМИ сообщили о переносе.",
                    "verbatim": "Мария Петрова написала @person о переносе.",
                    "date": "2026-01-30",
                    "source_n": 1,
                    "kind": "claim",
                    "who_says": "Мария Петрова",
                },
                {
                    "text": "Компания объяснила геологические работы.",
                    "verbatim": "Компания объяснила геологические работы.",
                    "date": "2026-02-05",
                    "source_n": 2,
                    "kind": "fact",
                },
            ]
        },
        "private_names": ["Мария Петрова"],
        "sources": [
            {"n": 1, "contour": "media", "date": "2026-01-30", "text": "RAW BODY"},
            {"n": 2, "contour": "media", "date": "2026-02-05", "messages": ["PRIVATE"]},
        ],
        "services": [
            {"id": "S02", "name": "Коммуникации с дольщиками", "purpose": "Единая позиция"},
            {"id": "S04", "name": "Чужая услуга", "purpose": "UNUSED"},
        ],
        "methods": [
            {"id": "M01", "name": "Сроки", "when": "Перенос", "steps": ["PRIVATE CASE"]},
            {"id": "M02", "name": "Чужой метод", "steps": ["UNUSED PERSON"]},
        ],
        "matrix_cards": [
            {"move": "A01", "service": "Коммуникации с дольщиками", "limits": "Согласование"},
            {"move": "I01", "solution": "UNUSED EXAMPLE"},
        ],
        "matrix_selection": {"picked": [{"move": "A01"}], "reserve": [{"move": "I01"}]},
        "products": {"products": {"position": "Коммуникации с дольщиками", "other": "UNUSED"}},
        "qualification": {"current_stage": 3, "research_evidence": "UNUSED RAW"},
        "public_communities": ["https://t.me/public"],
        "monitoring_scale": {"comparison_tolerance": 1},
        "report_scope": {"phase": "Этап 1.7"},
        "text_rules": {"stop_phrases": ["служебная оговорка"]},
    }
    before = copy.deepcopy(context)
    result = review.compact_review_context(report, context)
    assert result["report"] == report
    assert len(result["facts"]["facts"]) == 2  # Uncited company explanation is still checked.
    assert result["facts"]["facts"][1] == context["facts"]["facts"][1]
    assert result["facts"]["facts"][0]["verbatim"] == "[имя] написала [ник] о переносе."
    assert "who_says" not in result["facts"]["facts"][0]
    assert result["sources"] == [
        {"n": 1, "contour": "media", "date": "2026-01-30"},
        {"n": 2, "contour": "media", "date": "2026-02-05"},
    ]
    assert result["services"] == [context["services"][0]]
    assert result["methods"] == [{"id": "M01", "name": "Сроки", "when": "Перенос"}]
    assert result["allowed_moves"] == ["A01", "I01"]
    assert result["used_move_conditions"] == [context["matrix_cards"][0]]
    assert result["products"] == {"position": "Коммуникации с дольщиками"}
    assert result["qualification"] == {"current_stage": 3}
    assert result["report_scope"] == {"phase": "Этап 1.7"}
    assert result["text_rules"] == context["text_rules"]
    assert "public_communities" not in result
    assert context == before
    report["offer"]["modules"][0]["move"] = "I01"
    assert review.compact_review_context(report, context)["public_communities"] == [
        "https://t.me/public"
    ]


def test_final_assembled_prompts_include_scoped_critique_and_strategic_offer_rules():
    # These final constants guard against the earlier OFFER override dropping rules.
    assert prompts.OFFER.endswith(prompts.STRATEGIC_OFFER_RULES)
    assert prompts.CRITIC.endswith(prompts.STRATEGIC_CRITIC_RULES)
    assert "why_now — три фактические причины" in prompts.STRATEGIC_OFFER_RULES
    assert "report — единственный проверяемый текст" in prompts.STRATEGIC_CRITIC_RULES
    assert "/offer/modules/0/solution" in prompts.STRATEGIC_CRITIC_RULES
