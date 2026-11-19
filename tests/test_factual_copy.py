"""Copy formats may change; date precision, citation scope and actors may not."""

from copy import deepcopy

import pytest

from radar.reports.factual_copy import (
    name_project_deadline,
    normalize_proven_delay_duration,
    repair_context,
    strip_buyer_note_agency_work,
    validate_construction_scope,
    validate_copy_numbers,
    validate_handover_scope,
    validate_readiness_scope,
    validate_situation,
)


def fact(text, *, n=1, date=None, verbatim=None):
    return {"text": text, "date": date, "source_n": n, "kind": "fact",
            "who_says": "Источник", "verbatim": verbatim if verbatim is not None else text}


def test_buyer_note_drops_standalone_agency_work_from_report_285():
    note = (
        "Дольщики сомневаются в реалистичности нового срока. На форуме pronovostroy.ru "
        "сообщили об обращении собственников в прокуратуру и Мосгосстройнадзор — "
        "подтверждения от ведомств в открытых источниках нет."
    )
    assert strip_buyer_note_agency_work(
        note + " Агентство подготовит справку для самостоятельной подачи запросов в надзор."
    ) == note
    assert strip_buyer_note_agency_work(
        "Агентство готовит согласованную справку. " + note
    ) == note


@pytest.mark.parametrize("note", [
    "Агентство подготовит справку для дольщиков.",
    "Сроки опубликованы. Агентство готовит справку для покупателей.",
    "Дольщики ответили: «Ждём позицию. Агентство подготовит справку».",
    'Покупатель уточнил: "Компенсация неизвестна. Агентство готовит документы".',
    "Дольщики сомневаются. При необходимости агентство подготовит справку.",
])
def test_buyer_note_preserves_quotes_and_does_not_delete_whole_meaning(note):
    assert strip_buyer_note_agency_work(note) == note


def test_nearest_label_identifies_the_key_deadline_from_report_285():
    rows = [
        fact("Срок сдачи II квартал 2027."),
        fact("Застройщик перенёс срок выдачи ключей с марта 2026 на декабрь 2027.", n=3),
        fact("Теперь ключи готовы выдавать до 31.12.2027 года.", n=4),
    ]
    text = "Ближайший документированный срок — декабрь 2027 года"
    assert name_project_deadline(text, rows) == "Срок выдачи ключей — декабрь 2027 года"
    assert name_project_deadline(text, rows[1:]) == text
    assert name_project_deadline(text, rows[:1]) == text


@pytest.mark.parametrize("text", [
    "Ближайший документированный срок — март 2026 года",
    "Ближайший документированный срок — II квартал 2027 года",
    "Ближайший документированный срок — ноябрь 2027 года",
    "Ближайший документированный срок — декабрь 2027 года, затем открытие торгового центра",
    "Новый срок — декабрь 2027 года",
])
def test_deadline_normalization_does_not_relabel_other_source_dates_or_prose(text):
    rows = [
        fact("Срок сдачи II квартал 2027."),
        fact("Застройщик перенёс срок выдачи ключей с марта 2026 на декабрь 2027.", n=3),
    ]
    assert name_project_deadline(text, rows) == text


@pytest.mark.parametrize("copy", [
    "5 февраля 2026 опубликовано объяснение.",
    "05 февраля 2026 опубликовано объяснение.",
    "05.02.2026 опубликовано объяснение.",
    "2026-02-05 опубликовано объяснение.",
])
def test_day_date_formatting_is_not_a_new_number(copy):
    rows = [fact("Компания объяснила задержку.", date="2026-02-05")]
    validate_copy_numbers(copy, rows, [1])


def test_metric_month_year_is_a_narrow_presentation_equivalence():
    rows = [fact("Ранее ключи планировали в марте 2026 года.", date="Март 2026")]
    validate_copy_numbers("03.2026", rows, [1], metric_value=True)
    with pytest.raises(ValueError):
        validate_copy_numbers("Срок 03.2026", rows, [1])
    with pytest.raises(ValueError):
        validate_copy_numbers("03.2026", rows, [1], metric_value=False)


def test_abbreviated_metric_month_requires_that_month_in_cited_facts():
    rows = [fact("Ключи планируют в декабре 2027 года."), fact("Сдача в ноябре 2027.", n=2)]
    validate_copy_numbers("Дек. 2027", rows, [1], metric_value=True)
    for value in ("Нояб. 2027", "31 дек. 2027", "дек. 2028"):
        with pytest.raises(ValueError, match="точностью"):
            validate_copy_numbers(value, rows, [1], metric_value=True)
    for coarse in ("Ключи в 2027 году.", "Ключи в IV квартале 2027 года."):
        with pytest.raises(ValueError, match="точностью"):
            validate_copy_numbers("дек. 2027", [fact(coarse)], [1], metric_value=True)


@pytest.mark.parametrize("copy", ["2026-03-01", "1 марта 2026", "31.03.2026"])
def test_month_or_quarter_cannot_supply_a_precise_day(copy):
    with pytest.raises(ValueError, match="точностью"):
        validate_copy_numbers(copy, [fact("Сдача в марте 2026 года.")], [1])
    with pytest.raises(ValueError, match="точностью"):
        validate_copy_numbers(copy, [fact("Сдача в I квартале 2026 года.")], [1])


def test_no_arithmetic_or_rounding_and_numbers_are_scoped_to_cited_facts():
    rows = [fact("Готовность 37%.", n=1), fact("Участников 658.", n=2)]
    validate_copy_numbers("Готовность 37 процентов.", rows, [1])
    with pytest.raises(ValueError):
        validate_copy_numbers("658 покупателей.", rows, [1], path="buyers")
    with pytest.raises(ValueError):
        validate_copy_numbers("Готовность 38%.", rows, [1])
    with pytest.raises(ValueError):
        validate_copy_numbers("4 обращения.", [fact("2 обращения и ещё 2 обращения.")], [1])


def test_building_number_cannot_be_relabelled_as_a_percentage():
    with pytest.raises(ValueError, match="процент"):
        validate_copy_numbers("Готовность 37%.", [fact("Строится корпус 37.")], [1])


def test_658_group_size_plus_another_appeal_atom_does_not_create_658_applicants():
    rows = [
        fact("658 дольщиков сомневаются в сроке."),
        fact("Покупатели коллективно обратились в прокуратуру."),
    ]
    with pytest.raises(ValueError, match="одним точным фактом"):
        validate_copy_numbers("658 дольщиков коллективно обратились в прокуратуру.", rows, [1])
    validate_copy_numbers("Покупатели коллективно обратились в прокуратуру.", rows, [1])


@pytest.mark.parametrize("copy", [
    "Дольщики (658 чел.) коллективно обратились в прокуратуру.",
    "658 чел. коллективно обратились в прокуратуру.",
    "Дольщики (658) коллективно обратились в прокуратуру.",
])
def test_parenthesized_or_abbreviated_people_do_not_bypass_atomic_action_binding(copy):
    rows = [fact("658 дольщиков сомневаются в сроках.", n=1),
            fact("Покупатели коллективно обратились в прокуратуру.", n=2)]
    with pytest.raises(ValueError, match="одним точным фактом"):
        validate_copy_numbers(copy, rows, [1, 2])
    validate_copy_numbers(copy, [fact(copy)], [1])


def test_658_and_700_are_not_interchangeable_even_within_one_cited_atom():
    rows = [fact("658 дольщиков сомневаются в сроке, а 700 покупателей обратились в прокуратуру.")]
    validate_copy_numbers("700 покупателей обратились в прокуратуру.", rows, [1])
    with pytest.raises(ValueError, match="одним точным фактом"):
        validate_copy_numbers("658 покупателей обратились в прокуратуру.", rows, [1])


def test_family_size_does_not_become_number_of_applicants():
    rows = [fact("5 семей сомневаются в сроке. Покупатели подали коллективное обращение.")]
    with pytest.raises(ValueError, match="одним точным фактом"):
        validate_copy_numbers("5 семей подали коллективное обращение.", rows, [1])


def test_people_and_signature_or_appeal_counts_are_not_the_same_quantity():
    rows = [fact("658 дольщиков обсуждают срок. Покупатели направили коллективное обращение.")]
    with pytest.raises(ValueError, match="одним точным фактом"):
        validate_copy_numbers("Подано 658 коллективных обращений.", rows, [1])
    validate_copy_numbers("Подано 658 коллективных обращений.",
                          [fact("Подано 658 коллективных обращений.")], [1])


def test_model_paraphrase_cannot_support_its_own_invented_actor_action_link():
    rows = [fact("658 дольщиков обратились в прокуратуру.",
                 verbatim="658 дольщиков сомневаются в сроках.")]
    with pytest.raises(ValueError, match="одним точным фактом"):
        validate_copy_numbers("658 дольщиков обратились в прокуратуру.", rows, [1])


def test_date_in_sentence_about_appeals_is_not_treated_as_group_size():
    validate_copy_numbers("5 февраля 2026 опубликовано коллективное обращение.",
                          [fact("Опубликовано коллективное обращение.", date="2026-02-05")], [1])


@pytest.mark.parametrize("progress_source", [1, 2])
def test_handover_deadline_cannot_borrow_buildings_from_construction_progress(progress_source):
    rows = [
        fact("Выдачу ключей в ЖК перенесли с марта 2026 на декабрь 2027."),
        fact("В октябре 2026 готовность корпусов 1, 2 и 3 составила 37%.", n=progress_source),
    ]
    refs = sorted({1, progress_source})
    with pytest.raises(ValueError, match="дата выдачи ключей и указанный корпус"):
        validate_copy_numbers(
            "Выдачу ключей в корпусах 1–3 перенесли с марта 2026 на дек. 2027.", rows, refs,
        )
    validate_copy_numbers(
        "Выдачу ключей в ЖК перенесли с марта 2026 на дек. 2027. "
        "В октябре 2026 готовность корпусов 1–3 составила 37%.", rows, refs,
    )


def test_handover_scope_and_date_must_be_in_the_verbatim_claim():
    invented = fact("Корпус 3 получит ключи в декабре 2027.",
                    verbatim="Ключи в ЖК планируют на декабрь 2027. Корпус 3 строится.")
    actual = fact("Выдачу ключей в корпусах №1, №2 и №3 перенесли на декабрь 2027.", n=2)
    text = "Выдача ключей в корпусах 1–3 — дек. 2027."
    with pytest.raises(ValueError, match="одним точным фактом"):
        validate_handover_scope(text, [invented, actual], [1])
    validate_handover_scope(text, [invented, actual], [2])


def test_scope_range_before_building_label_must_be_supported_as_a_whole():
    rows = [fact("Выдачу ключей в корпусе 3 перенесли на декабрь 2027."),
            fact("Выдачу ключей в корпусах 1, 2 и 3 перенесли на декабрь 2027.", n=2)]
    text = "Выдачу ключей в 1–3 корпусах перенесли на декабрь 2027."
    with pytest.raises(ValueError, match="одним точным фактом"):
        validate_handover_scope(text, rows, [1])
    validate_handover_scope(text, rows, [2])


def test_phase_identifiers_and_building_identifiers_do_not_substitute_for_each_other():
    rows = [fact("В корпусе 2 первой очереди ключи начали выдавать 1 марта 2023 года."),
            fact("Выдача ключей в этапе 1.7 запланирована на декабрь 2027.")]
    validate_handover_scope("Ключи II корпуса первой очереди — 01.03.2023.", rows, [1])
    validate_handover_scope("Ключи этапа 1.7 — декабрь 2027.", rows, [1])
    for claim in ("Ключи корпуса 1.7 — декабрь 2027.", "Ключи 2-й очереди — март 2023.",
                  "Ключи корпуса 2 — декабрь 2027.", "Ключи корпуса 2 — 2 марта 2023."):
        with pytest.raises(ValueError, match="одним точным фактом"):
            validate_handover_scope(claim, rows, [1])


def test_separate_timeline_date_belongs_to_scoped_handover_fact():
    rows = [fact("Началась передача ключей во 2-м корпусе первой очереди.", date="2023-03-01"),
            fact("В корпусе 2 начали работы.", date="2026-03-01")]
    text = "Началась передача ключей в корпусе 2 первой очереди."
    validate_handover_scope(text, rows, [1], event_date="2023-03-01")
    with pytest.raises(ValueError, match="одним точным фактом"):
        validate_handover_scope(text, rows, [1], event_date="2026-03-01")


def test_project_readiness_cannot_borrow_scope_from_monolith_progress():
    rows = [
        fact("Готовность объекта достигла 37%.", date="2026-10-28"),
        fact("Выполняются монолитные работы в корпусах 1, 2 и 3.",
             verbatim="В настоящий момент активно выполняются монолитные работы выше нуля."),
    ]
    with pytest.raises(ValueError, match="процент готовности и указанный корпус"):
        validate_copy_numbers(
            "К октябрю 2026 готовность корпусов 1–3 составила 37%; дольщики сомневаются.",
            rows, [1],
        )
    validate_copy_numbers(
        "Готовность объекта 37%, в корпусах 1, 2 и 3 ведутся монолитные работы.", rows, [1],
    )
    other_clause = fact("В корпусах 1–3 ведутся монолитные работы, готовность объекта 37%.")
    with pytest.raises(ValueError, match="одним точным фактом"):
        validate_readiness_scope("Готовность корпусов 1–3 — 37%.", [other_clause], [1])


def test_early_foundation_cannot_borrow_buildings_from_later_progress():
    rows = [
        fact(
            "В начале 2026 года в корпусах 1–3 был только фундамент.",
            verbatim="Собственники говорят, что сейчас там есть лишь фундамент.",
        ),
        fact("28 октября 2026 в корпусах 1, 2 и 3 ведутся монолитные работы.", n=6),
        fact("В январе 2026 в корпусах 1–3 был только фундамент.", n=7),
    ]
    text = (
        "На начало 2026 года на площадке корпусов 1–3 был только фундамент; "
        "к октябрю 2026 готовность — 37% по данным «Эталона»."
    )
    with pytest.raises(ValueError, match=r"diagnosis\.main_problem\.causes\.1\.text: числа"):
        validate_construction_scope(
            text, rows, [1, 6], path="diagnosis.main_problem.causes.1.text",
        )
    validate_construction_scope(text, rows, [1, 6, 7])


@pytest.mark.parametrize(
    "next_date", [". В октябре", "; к октябрю", ", а к октябрю", ", к октябрю"]
)
def test_separate_later_update_preserves_grounded_single_building_foundation(next_date):
    rows = [
        fact("Дольщики сообщили, что в январе 2026 в корпусе №1 был лишь фундамент."),
        fact("В октябре 2026 в корпусах 1, 2 и 3 ведутся монолитные работы.", n=2),
    ]
    copy = (
        "В январе 2026 дольщики сообщили, что в корпусе 1 был только фундамент"
        f"{next_date} 2026 в корпусах 1–3 начались монолитные работы."
    )
    validate_construction_scope(copy, rows, [1, 2])
    validate_construction_scope(
        "В январе 2026 дольщики сообщили, что на площадке был только фундамент; "
        "к октябрю 2026 в корпусах 1–3 начались монолитные работы.",
        rows, [1, 2],
    )


def test_unknown_building_cannot_borrow_scope_from_another_clause_of_the_same_fact():
    rows = [fact(
        "В январе 2026 был только фундамент, в октябре 2026 в корпусах 1–3 шли работы."
    )]
    with pytest.raises(ValueError, match="числа корпусов"):
        validate_construction_scope("В январе 2026 в корпусах 1–3 был лишь фундамент.", rows, [1])
    validate_construction_scope("В январе 2026 у корпуса 1 был не только фундамент.", rows, [1])


def test_readiness_percentage_matches_exact_scope_and_source():
    rows = [fact("В корпусах №1, №2 и №3 выполнено 37,5% работ.", n=2),
            fact("Готовность объекта — 37,5 процента.")]
    validate_readiness_scope("Готовность 1–3 корпусов — 37,50%.", rows, [2])
    with pytest.raises(ValueError, match="одним точным фактом"):
        validate_readiness_scope("Готовность 1–3 корпусов — 37,5%.", rows, [1])
    for claim in ("Корпус 1 готов на 37,5%.", "Корпуса 1–3 готовы на 37%."):
        with pytest.raises(ValueError, match="одним точным фактом"):
            validate_readiness_scope(claim, rows, [2])


def test_delay_normalization_copies_the_explicit_duration_from_a_cited_fact():
    rows = [fact("Перенос срока составил 1 год и 9 месяцев.", n=4)]
    text = "Срок сдвинулся — перенос составил 21 мес. Причина обсуждается."
    normalized = normalize_proven_delay_duration(text, rows, [4])
    assert normalized == "Срок сдвинулся — перенос составил 1 год 9 месяцев. Причина обсуждается."
    validate_copy_numbers(normalized, rows, [4])
    assert normalize_proven_delay_duration(text, rows, [3]) == text


def test_delay_normalization_keeps_scope_and_occurrence_type():
    rows = [fact("Для корпуса 1 перенос передачи ключей составил 1 г. 9 мес.")]
    assert normalize_proven_delay_duration(
        "Для корпуса 1 сдвиг выдачи ключей на 21 месяц.", rows, [1],
    ) == "Для корпуса 1 сдвиг выдачи ключей на 1 г. 9 мес."
    for claim in ("Для корпуса 2 сдвиг выдачи ключей на 21 месяц.",
                  "Для корпуса 1 сдвиг ввода на 21 месяц.",
                  "Сдвиг выдачи ключей на 21 месяц."):
        assert normalize_proven_delay_duration(claim, rows, [1]) == claim


def test_delay_normalization_does_not_calculate_dates_or_reuse_unrelated_amounts():
    rows = [fact("Срок перенесли с марта 2026 на декабрь 2027. Договор на 1 год и 9 месяцев.")]
    exact = "Сдвиг на 21 месяц."
    assert normalize_proven_delay_duration(exact, rows, [1]) == exact
    rows.append(fact("Задержка составила 1 год и 9 месяцев."))
    for claim in ("Сдвиг на 22 месяца.", "Сдвиг на 21,5 месяца.",
                  "Сдвиг примерно на 21 месяц.", "Договор на 21 месяц."):
        assert normalize_proven_delay_duration(claim, rows, [1]) == claim


def test_offer_repair_context_uses_problem_refs_in_unwrapped_offer():
    rows = [fact("Выдачу ключей перенесли в декабре 2027."), fact("Корпус 2 достроен.", n=2)]
    offer = {"modules": [{"pain": "Ключи корпуса 2 — декабрь 2027.", "pain_refs": [1]}]}
    repair = repair_context("offer.modules.0.pain: дата выдачи ключей", rows, previous=offer)
    assert repair["cited_refs"] == [1]
    assert [item["source_n"] for item in repair["supported_facts"]] == [1]


def situation():
    return {
        "lead": "Есть спор об обязательствах.",
        "facts": [
            {"value": "03.2026", "label": "Прежний ориентир ключей", "refs": [1], "tone": "alert"},
            {"value": "2027", "label": "Новый ориентир", "refs": [1], "tone": "alert"},
            {"value": "37%", "label": "Готовность на 28 октября 2026", "refs": [2],
             "tone": "neutral"},
            {"value": "2027", "label": "Плановый год ввода", "refs": [1], "tone": "neutral"},
        ],
        "timeline": [
            {"date": "28.10", "date_iso": "2026-10-28", "text": "Готовность 37%.",
             "refs": [2], "tone": "neutral"},
            {"date": "05.02.2026", "date_iso": "2026-02-05", "text": "Компания дала объяснение.",
             "refs": [3], "tone": "neutral"},
            {"date": "30 января 2026", "date_iso": "2026-01-30",
             "text": "Опубликован материал о сроках.", "refs": [1], "tone": "alert"},
        ],
        "buyers": {
            "status": "Есть претензии", "note": "Покупатель пишет о переносе срока.",
            "note_refs": [4],
            "quotes": [{"text": "В моём договоре стоит 31.12.2026, позже срок перенесли.",
                        "attribution": "Форум, 16.03.2026", "refs": [4]}],
            "topics": [],
        },
        "company": {"status": "Есть позиция", "points": [
            {"text": "Компания дала объяснение 5 февраля 2026.", "refs": [3]}
        ]},
        "gap": "Путь к передаче",
    }


@pytest.fixture
def proof():
    rows = [
        fact("Ключи планировали в марте 2026, позднее — в декабре 2027.", date="2026-01-30"),
        fact("Готовность 37%.", n=2, date="28 октября 2026"),
        fact("Компания дала объяснение.", n=3, date="2026-02-05"),
        fact("В моём договоре стоит 31.12.2026, позже срок перенесли.",
             n=4, date="16 марта 2026"),
    ]
    selected = [{"n": n, "text": "\n".join(f["verbatim"] for f in rows if f["source_n"] == n)}
                for n in range(1, 5)]
    return rows, selected


def test_situation_keeps_exact_copy_and_sorts_only_the_timeline(proof):
    rows, selected = proof
    authored = situation()
    original = deepcopy(authored)
    result = validate_situation(authored, rows, selected)
    assert result.facts[0].value == "03.2026"
    assert [e.date_iso for e in result.timeline] == ["2026-01-30", "2026-02-05", "2026-10-28"]
    assert result.buyers.quotes[0].text == authored["buyers"]["quotes"][0]["text"]
    assert authored == original


def test_situation_cannot_borrow_number_from_uncited_source(proof):
    rows, selected = proof
    value = situation()
    value["facts"][2]["refs"] = [1]
    with pytest.raises(ValueError, match=r"facts.2.value"):
        validate_situation(value, rows, selected)


def test_situation_metric_scope_and_date_share_one_fact_even_in_separate_fields(proof):
    rows, selected = proof
    rows.append(fact("Готовность корпуса 3 оценивают отдельно."))
    value = situation()
    value["facts"][1].update(value="12.2027", label="Новый срок выдачи ключей корпуса 3")
    with pytest.raises(ValueError, match=r"facts\.1\.label: дата выдачи ключей"):
        validate_situation(value, rows, selected)


def test_situation_binds_readiness_tile_value_and_label(proof):
    rows, selected = proof
    rows.append(fact("В корпусе 3 выполняются монолитные работы.", n=2))
    value = situation()
    value["facts"][2]["label"] = "Готовность корпуса 3 на 28 октября 2026"
    with pytest.raises(ValueError, match=r"facts\.2\.label: процент готовности"):
        validate_situation(value, rows, selected)


def test_situation_tile_reuses_cited_delay_without_touching_its_label_or_refs(proof):
    rows, selected = proof
    rows.append(fact("Перенос срока составил 1 год и 9 месяцев."))
    authored = situation()
    authored["facts"][1].update(value="21 месяц", label="Суммарный перенос срока")
    original = deepcopy(authored)
    result = validate_situation(authored, rows, selected)
    assert result.facts[1].model_dump() == {**authored["facts"][1], "value": "1 год 9 месяцев"}
    assert authored == original
    authored["facts"][1]["label"] = "Период договора"
    with pytest.raises(ValueError, match=r"facts\.1\.value: Число"):
        validate_situation(authored, rows, selected)


def test_timeline_date_iso_needs_scoped_fact_proof_not_page_metadata(proof):
    rows, selected = proof
    value = situation()
    selected[2]["evidence_date"] = "2026-02-06"
    value["timeline"][1]["date"] = "06.02.2026"
    value["timeline"][1]["date_iso"] = "2026-02-06"
    with pytest.raises(ValueError, match="date_iso"):
        validate_situation(value, rows, selected)


def test_timeline_display_day_cannot_disagree_with_its_proven_iso_day(proof):
    rows, selected = proof
    value = situation()
    value["timeline"][0]["date"] = "27.10"
    with pytest.raises(ValueError, match="подпись даты"):
        validate_situation(value, rows, selected)


@pytest.mark.parametrize("display", ["6 февраля 2026", "06.02.2026", "6 февраля"])
def test_display_cannot_choose_another_supported_day_than_date_iso(proof, display):
    rows, selected = proof
    rows.append(fact("Другой ответ опубликован.", n=3, date="2026-02-06"))
    value = situation()
    value["timeline"][1]["date"] = display
    with pytest.raises(ValueError, match="подпись даты"):
        validate_situation(value, rows, selected)


def test_short_textual_day_month_displays_its_proven_day(proof):
    rows, selected = proof
    value = situation()
    value["timeline"][1]["date"] = "5 февраля"
    assert validate_situation(value, rows, selected).timeline[1].date == "5 февраля"


def test_invalid_iso_date_is_not_proved_by_separate_numbers(proof):
    rows, selected = proof
    rows.append(fact("В 2026 году было 99 обращений.", n=3))
    value = situation()
    value["timeline"][1]["date"] = "2026"
    value["timeline"][1]["date_iso"] = "2026-99-99"
    with pytest.raises(ValueError, match="некорректный date_iso"):
        validate_situation(value, rows, selected)


def test_precise_day_not_inferred_from_separate_day_number_and_month(proof):
    rows, selected = proof
    rows[2] = fact("В феврале 2026 было 5 обращений.", n=3)
    with pytest.raises(ValueError, match="date_iso"):
        validate_situation(situation(), rows, selected)


def test_quote_is_not_fuzzily_restored_from_source_or_rewritten(proof):
    rows, selected = proof
    value = situation()
    value["buyers"]["quotes"][0]["text"] = "В моем договоре стоит 31.12.2026, позже срок перенесли."
    with pytest.raises(ValueError, match="дословно"):
        validate_situation(value, rows, selected)


def test_real_page_quote_without_an_atomic_quote_fragment_is_not_admitted(proof):
    rows, selected = proof
    selected[3]["text"] += "\nЯ считаю, что срок нужно пересмотреть."
    value = situation()
    value["buyers"]["quotes"][0]["text"] = "Я считаю, что срок нужно пересмотреть."
    with pytest.raises(ValueError, match="дословно"):
        validate_situation(value, rows, selected)


def test_supported_topics_need_no_quotation(proof):
    rows, selected = proof
    value = situation()
    value["buyers"]["quotes"] = []
    value["buyers"]["topics"] = [{"text": "Перенос срока", "refs": [4]}]
    assert validate_situation(value, rows, selected).buyers.quotes == []


def test_long_prose_is_not_rejected_by_numeric_grounding(proof):
    rows, selected = proof
    value = situation()
    value["lead"] = "Нужно согласовать обязательства и регулярное общение с покупателями. " * 40
    assert validate_situation(value, rows, selected).lead == value["lead"]


def test_repair_hints_stay_inside_actual_failed_fields_refs_and_do_not_propose_foreign_proof():
    rows = [fact("Ключи планировали в декабре 2027.", n=1, date=None),
            fact("В корпусах 2 и 3 выполняются работы.", n=2, date="2026-10-28")]
    candidate = situation()
    candidate["facts"][1]["label"] = "Срок ключей по корпусам 2 и 3"
    candidate["facts"][1]["refs"] = [1]
    original = deepcopy(candidate)
    hints = repair_context("situation.facts.1.label: Число отсутствует: 2; 3",
                           rows, previous=candidate)
    assert hints["path"] == "situation.facts.1.label" and hints["cited_refs"] == [1]
    assert {row["source_n"] for row in hints["supported_facts"]} == {1}
    assert hints["date_options"] == []
    assert candidate == original and "Не добавляй чужую ссылку" in hints["instruction"]


def test_repair_hints_find_refs_in_full_report_and_expose_only_own_fact_dates():
    rows = [fact("Перенос ключей.", n=1, date="2026-01-30"),
            fact("Другое событие.", n=2, date="2024-07-26")]
    candidate = {"situation": situation()}
    candidate["situation"]["timeline"][0]["refs"] = [1]
    hints = repair_context("situation.timeline.0.date_iso: Дата не подтверждена: 2024-Q3",
                           rows, previous=candidate)
    assert hints["cited_refs"] == [1]
    assert [row["date"] for row in hints["date_options"]] == ["2026-01-30"]


def test_unscoped_repair_hints_keep_source_identity_and_do_not_change_validation():
    rows = [fact("Срок в декабре 2027.", n=1, date=None)]
    hints = repair_context(ValueError("Общая ошибка без адреса поля"), rows)
    assert hints["path"] is None and hints["cited_refs"] is None
    assert hints["supported_facts"][0]["source_n"] == 1
    with pytest.raises(ValueError):
        validate_copy_numbers("Работы в корпусе 9.", rows, [1])


@pytest.mark.parametrize("page,path", [
    ({"diagnosis": {"main_problem": {"causes": [{"text": "Через 14 месяцев", "refs": [1]}]}}},
     "diagnosis.main_problem.causes.0.text"),
    ({"triggers": [{"text": "Через 14 месяцев", "refs": [1]}]}, "scenario.triggers.0.text"),
])
def test_diagnostic_and_scenario_repairs_do_not_borrow_numbers_from_foreign_facts(page, path):
    rows = [fact("В октябре 2026 готовность составила 37%.", n=1, date="2026-10-28"),
            fact("Участник назвал 14,5 млрд рублей.", n=2, date=None)]
    hints = repair_context(path + ": Число не подтверждено: 14", rows, previous=page)
    assert hints["cited_refs"] == [1]
    assert {row["source_n"] for row in hints["supported_facts"]} == {1}
    assert "Не вычисляй новые интервалы" in hints["instruction"]


@pytest.mark.parametrize("block,path", [
    ("buyers", "situation.buyers.topics.0.text"),
    ("company", "situation.company.points.0.text"),
])
def test_copy_failures_address_a_concrete_text_field_for_targeted_repair(proof, block, path):
    rows, selected = proof
    value = situation()
    if block == "buyers":
        value["buyers"]["topics"] = [{"text": "Подано 999 обращений", "refs": [1]}]
    else:
        value["company"]["points"] = [{"text": "Подано 999 обращений", "refs": [1]}]
    with pytest.raises(ValueError) as caught:
        validate_situation(value, rows, selected)
    assert str(caught.value).startswith(path + ":")
    hints = repair_context(caught.value, rows, previous=value)
    assert hints["cited_refs"] == [1]
