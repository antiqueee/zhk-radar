"""Check selected semantic distinctions that failed in the Nagatino report."""

import pytest

from radar.reports.editorial_evidence import editorial_claim_issues, ground_observation_copy


def fact(verbatim, n=1):
    return {"source_n": n, "verbatim": verbatim}


def offer(**module):
    return {"offer": {"modules": [module]}}


def test_group_ddu_receipt_needs_more_than_a_project_deadline_or_individual_case():
    rows = [
        fact("Перенесён срок выдачи ключей в ЖК с марта 2026 на декабрь 2027."),
        fact("Одна из дольщиц получила изменённые условия по ДДУ."),
        fact("Дольщикам направили изменённые условия по ДДУ."),
        fact("Изменённые условия по ДДУ дольщиками не были получены."),
        fact("Дольщики получили изменённые условия по ДДУ.", n=2),
    ]
    blocks = {"scenario": {"triggers": [{
        "text": "Выдача ключей сдвинута — дольщики получили изменённые условия по ДДУ.",
        "refs": [1],
    }]}}
    [issue] = editorial_claim_issues(blocks, rows)
    assert issue["path"] == "scenario.triggers.0.text" and issue["refs"] == [1]
    assert "сообщение одного владельца" in issue["reason"]
    blocks["scenario"]["triggers"][0]["refs"] = [2]
    assert editorial_claim_issues(blocks, rows) == []
    blocks["scenario"]["triggers"][0].update(
        text="Одна из дольщиц сообщила, что получила изменённые условия по ДДУ.", refs=[1],
    )
    assert editorial_claim_issues(blocks, rows) == []


def test_reported_filing_scope_must_be_in_the_same_source_claim():
    rows = [
        fact("Собственники коллективно обратились в прокуратуру и СК."),
        fact("В корпусах 1–3 идут монолитные работы."),
        fact("Собственники корпусов 1, 2 и 3 коллективно обратились в прокуратуру и СК.", n=2),
    ]
    pain = "На форуме сообщили о коллективном обращении дольщиков корпусов 1–3 в прокуратуру и СК."
    [issue] = editorial_claim_issues(offer(pain=pain, pain_refs=[1]), rows)
    assert issue["path"] == "offer.modules.0.pain" and issue["kind"] == "editorial_claim"
    assert editorial_claim_issues(offer(pain=pain, pain_refs=[2]), rows) == []
    separate = ("На форуме сообщили об обращении дольщиков в прокуратуру и СК, "
                "и в корпусах 1–3 ведутся монолитные работы.")
    assert editorial_claim_issues(offer(pain=separate, pain_refs=[1]), rows) == []
    assert editorial_claim_issues(offer(pain=pain, pain_refs=[1]), [fact(separate)])


@pytest.mark.parametrize("text", [
    "Агентство ведёт переговоры с инициативной группой.",
    "Выходим на инициативную группу до следующего обращения.",
    "Инициативная группа уже действует.",
])
def test_filing_does_not_prove_an_initiative_group(text):
    rows = [fact("Дольщики обратились в прокуратуру."),
            fact("Инициативная группа не действует.")]
    [issue] = editorial_claim_issues(offer(solution=text, pain_refs=[1]), rows)
    assert issue["path"] == "offer.modules.0.solution"
    assert "Коллективное обращение этого не доказывает" in issue["reason"]
    rows.append(fact("Дольщики сформировали инициативную группу."))
    assert editorial_claim_issues(offer(solution=text, pain_refs=[1]), rows) == []


def test_group_search_conditional_contact_and_catalog_labels_remain_available():
    module = {
        "name": "Переговоры с инициативной группой",
        "service": "Работа с инициативной группой",
        "agency": ["Ищем добровольных представителей дольщиков."],
        "solution": "При наличии инициативной группы организуем переговоры с её участниками.",
        "outcome": "Проводим встречу с инициативной группой, если подтвердится её состав.",
    }
    assert editorial_claim_issues(offer(**module), []) == []
    module["solution"] = "Агентство создаёт инициативную группу из откликнувшихся."
    assert editorial_claim_issues(offer(**module), []) == []


def test_negative_communication_claims_need_specific_evidence():
    rows = [fact("Форум обсуждает перенос сроков."),
            fact("Дольщики не имеют единого канала для ответов.", n=2)]
    module = {
        "pain": ("Дольщики не имеют единого канала для ответов. "
                 "Требования не переведены в переговоры."),
        "pain_refs": [1, 2],
        "solution": "Проверим, есть ли единый канал для ответов. Предложим согласованный порядок.",
    }
    [issue] = editorial_claim_issues(offer(**module), rows)
    assert issue["path"] == "offer.modules.0.pain"
    assert "требования остались без переговоров" in issue["reason"]
    assert "отсутствие единого канала" not in issue["reason"]
    module["pain_refs"] = [1]
    assert "отсутствие единого канала" in editorial_claim_issues(offer(**module), rows)[0]["reason"]


@pytest.mark.parametrize("pain", [
    "Позиция застройщика по срокам и ходу работ в корпусах 1–3 пока не сведена в единый документ.",
    "Требования дольщиков по срокам остаются без предметного ответа.",
    "Единого содержательного ответа на коллективные претензии в источниках не зафиксировано.",
    "Требования пока не переведены в предметный диалог.",
    "Позиция застройщика для ответа на возможные запросы ведомств не подготовлена.",
])
def test_one_nonresponse_does_not_prove_absence_of_a_document_or_collective_reply(pain):
    rows = [fact("Застройщик не прокомментировал запрос Readovka.")]
    module = {"pain": pain, "pain_refs": [1]}
    assert [row["path"] for row in editorial_claim_issues(offer(**module), rows)] == [
        "offer.modules.0.pain",
    ]
    assert editorial_claim_issues(offer(**module), [fact(pain)]) == []
    module["pain"] = "Уточним, подготовлен ли единый документ и отвечали ли на претензии."
    assert editorial_claim_issues(offer(**module), rows) == []


def test_contract_deadline_for_several_ddu_requires_the_same_date_for_several_contracts():
    rows = [fact("В моём ДДУ указан срок 31.12.2026."),
            fact("В нескольких ДДУ указан срок 31 декабря 2026 года.", n=2),
            fact("В нескольких ДДУ указан срок 30 декабря 2026 года.", n=3)]
    pain = "Часть ДДУ содержала срок 31.12.2026."
    assert editorial_claim_issues(offer(pain=pain, pain_refs=[1]), rows)
    assert editorial_claim_issues(offer(pain=pain, pain_refs=[3]), rows)
    assert editorial_claim_issues(offer(pain=pain, pain_refs=[2]), rows) == []
    individual = "Одна дольщица сообщила о сроке 31.12.2026 в своём ДДУ."
    assert editorial_claim_issues(offer(pain=individual, pain_refs=[1]), rows) == []


@pytest.mark.parametrize("pain", [
    "Часть дольщиков Nagatino I-Land указывает на расхождение между договорным сроком "
    "(31.12.2026) и новым сроком выдачи ключей (31.12.2027).",
    "Группа дольщиков указывает на договорный срок 31.12.2026.",
    "У покупателей в договорах стоит 31.12.2026.",
])
def test_collective_contract_date_is_detected_without_ddu_acronym(pain):
    rows = [
        fact("В моём договоре стоит 31.12.2026г."),
        fact("У нескольких собственников в договорах стоит 31 декабря 2026 года.", n=2),
        fact("У покупателей в договорах стоит 30 декабря 2026 года.", n=3),
        fact("В нескольких ДДУ не стоял срок 31.12.2026.", n=4),
    ]
    assert editorial_claim_issues(offer(pain=pain, pain_refs=[1, 3, 4]), rows)
    assert editorial_claim_issues(offer(pain=pain, pain_refs=[2]), rows) == []


def test_individual_deadline_and_date_of_contract_signing_are_not_collective_deadlines():
    individual = "Одна из дольщиц говорит о договорном сроке 31.12.2026."
    signing = "Несколько ДДУ подписано 31.12.2026."
    newer = "В договорах покупателей срок обсуждается, но официальный срок указан 31.12.2027."
    for pain in (individual, signing, newer):
        assert editorial_claim_issues(offer(pain=pain), []) == []


@pytest.mark.parametrize("claim,verbatim", [
    ("Дольщики видят 37% готовности и публично сомневаются, что застройщик уложится в срок.",
     "Дольщики прочитали о 37 процентах готовности и сомневаются в сроке."),
    ("При 37% готовности к октябрю 2026 дольщики публично оспаривают выполнимость нового срока.",
     "При готовности 37% покупатели оспаривают выполнимость нового срока."),
    ("Дольщики сомневаются в реалистичности нового срока: на октябрь 2026 года "
     "готовность объекта составляет 37%.",
     "Готовность в 37% вызвала сомнения у дольщиков в новом сроке."),
])
def test_buyer_reaction_must_be_linked_to_the_same_readiness_percentage(claim, verbatim):
    rows = [fact("В феврале 2026 дольщики сомневались в новом сроке."),
            fact("Готовность объекта на октябрь 2026 достигла 37%."),
            fact(verbatim, n=2)]
    blocks = {"scenario": {"triggers": [{"text": claim, "refs": [1]}]}}
    [issue] = editorial_claim_issues(blocks, rows)
    assert issue["path"] == "scenario.triggers.0.text"
    assert "этот процент готовности" in issue["reason"]
    blocks["scenario"]["triggers"][0]["refs"] = [2]
    assert editorial_claim_issues(blocks, rows) == []


def test_scenario_pace_doubt_requires_same_scope_evidence_and_allows_a_future_risk():
    text = "Низкий темп строительства усиливает сомнения в реалистичности нового срока."
    rows = [fact("Готовность объекта к октябрю достигла 37%, работы идут по графику."),
            fact("В феврале дольщики сомневались в новом сроке."),
            fact("Медленный темп стройки вызвал сомнения дольщиков в новом сроке.", n=2)]
    trigger = {"text": text, "refs": [1]}
    blocks = {"scenario": {"triggers": [trigger]}}
    [issue] = editorial_claim_issues(blocks, rows)
    assert issue["path"] == "scenario.triggers.0.text"
    assert "такой темп стройки" in issue["reason"]
    trigger["refs"] = [2]
    assert editorial_claim_issues(blocks, rows) == []
    trigger.update(text="Низкий темп строительства может усилить сомнения в новом сроке.", refs=[1])
    assert editorial_claim_issues(blocks, rows) == []


@pytest.mark.parametrize("text", [
    "Если требования пока не переведены в предметный диалог, организуем обсуждение.",
    "Проверим, подготовлена ли позиция застройщика для ответа на возможные запросы ведомств.",
    "Нужно согласовать позицию застройщика для ответа на возможные запросы ведомств.",
    "Предлагаем перевести требования в предметный диалог и подготовить ответ ведомствам.",
])
def test_known_communication_gaps_leave_checks_and_future_actions(text):
    assert editorial_claim_issues(offer(pain=text), []) == []


@pytest.mark.parametrize("text", [
    "На октябрь 2026 готовность объекта 37%. На форуме в феврале дольщики сомневались в сроке.",
    "На октябрь 2026 готовность объекта 37%; в феврале дольщики сомневались в сроке.",
    "Дольщики узнали о переносе, готовность объекта в октябре составила 37%.",
    "Дольщики сомневались после переноса, а готовность объекта в октябре составила 37%.",
])
def test_separately_attributed_build_readiness_and_buyers_doubts_do_not_imply_a_reaction(text):
    assert editorial_claim_issues(offer(pain=text), []) == []


@pytest.mark.parametrize("pain", [
    "Опережающей справки для ведомств в открытых источниках не зафиксировано.",
    "Содержательного публичного ответа по существу обращений в источниках не зафиксировано.",
])
def test_public_document_or_reply_absence_requires_matching_evidence(pain):
    rows = [fact("Позиция застройщика по геологии раскрыта в СМИ.")]
    [issue] = editorial_claim_issues(offer(pain=pain, pain_refs=[1]), rows)
    assert issue["path"] == "offer.modules.0.pain"
    assert editorial_claim_issues(offer(pain=pain, pain_refs=[1]), [fact(pain)]) == []
    future = "Проверим, опубликована ли справка. Подготовим содержательный публичный ответ."
    assert editorial_claim_issues(offer(solution=future, pain_refs=[1]), rows) == []


def test_collective_direct_contact_with_the_developer_is_not_a_past_authority_filing():
    text = "Ключи сдвинуты на декабрь 2027 — дольщики обращаются к застройщику напрямую."
    rows = [fact("На форуме сообщили об обращении собственников в прокуратуру."),
            fact("Дольщики не обращаются к застройщику."),
            fact("Дольщики обратились к застройщику в феврале 2026."),
            fact("Собственники обращаются напрямую к застройщику.", n=2)]
    blocks = {"scenario": {"triggers": [{"text": text, "refs": [1]}]}}
    [issue] = editorial_claim_issues(blocks, rows)
    assert issue["path"] == "scenario.triggers.0.text"
    blocks["scenario"]["triggers"][0]["refs"] = [2]
    assert editorial_claim_issues(blocks, rows) == []
    solution = "Если дольщики обращаются к застройщику, предлагаем согласовать порядок ответов."
    assert editorial_claim_issues(offer(solution=solution, pain_refs=[1]), rows) == []


@pytest.mark.parametrize("text,expected", [
    ("Ключи сдвинуты с марта 2026 на декабрь 2027 — дольщики требуют объяснений напрямую.",
     "Ключи сдвинуты с марта 2026 на декабрь 2027."),
    ("Ключи сдвинуты с марта 2026 на декабрь 2027 — дольщики обращаются к застройщику напрямую.",
     "Ключи сдвинуты с марта 2026 на декабрь 2027."),
    ("К октябрю 2026 года готовность — 37%; сомнения в реалистичности нового срока усиливаются.",
     "К октябрю 2026 года готовность — 37%."),
])
def test_ground_observation_keeps_the_verified_deadline_or_readiness(text, expected):
    rows = [fact("Выдачу ключей перенесли с марта 2026 на декабрь 2027."),
            fact("Готовность на 28 октября 2026 достигла 37%."),
            fact("На форуме в феврале дольщики сомневались в сроке."),
            fact("Дольщики требуют объяснений напрямую.", n=2)]
    assert ground_observation_copy(text, rows, [1]) == expected


def test_ground_observation_preserves_cited_contact_and_unsupported_or_conditional_heads():
    rows = [fact("Выдачу ключей перенесли на декабрь 2027."),
            fact("Дольщики требуют объяснений напрямую.", n=2)]
    text = "Ключи перенесены на декабрь 2027 — дольщики требуют объяснений напрямую."
    assert ground_observation_copy(text, rows, [1, 2]) == text
    assert ground_observation_copy(text, rows, [2]) == text  # No independently supported head.
    for candidate in (
        "Дольщики требуют объяснений напрямую.",
        "Если ключи перенесены на декабрь 2027 — дольщики требуют объяснений напрямую.",
        "Ключи перенесены на декабрь 2027 — проверим, обращаются ли дольщики напрямую.",
    ):
        assert ground_observation_copy(candidate, rows, [1]) == candidate


@pytest.mark.parametrize("head,tail", [
    ("На 28 октября 2026 года готовность объекта составляет 37%",
     ", что усиливает сомнения в новом сроке."),
    ("По данным стройотчёта застройщика от 28 октября 2026 года, готовность объекта достигла 37%",
     ". Ранее, 30 января 2026 года, люди выражали сомнения в соблюдении нового срока — "
     "на тот момент на площадке находился только фундамент."),
])
def test_optional_offer_readiness_repair_keeps_exact_grounded_head(head, tail):
    rows = [
        {**fact("Готовность объекта на сегодняшний день достигла 37%.", 6),
         "kind": "fact", "date": "2026-10-28"},
        {**fact("На площадке находится только фундамент, что вызывает сомнения у дольщиков.", 3),
         "kind": "claim", "date": "2026-01-30"},
    ]
    text = head + tail
    assert ground_observation_copy(text, rows) == text
    assert ground_observation_copy(text, rows, repair_readiness=True) == head + "."
    assert ground_observation_copy(text, rows, [3], repair_readiness=True) == text
    for unsupported in (
        text.replace("37%", "38%"),
        text.replace("готовность объекта", "готовность корпусов 1–3"),
        text.replace("28 октября", "29 октября"),
    ):
        assert ground_observation_copy(unsupported, rows, repair_readiness=True) == unsupported


def test_optional_offer_readiness_repair_preserves_evidence_backed_and_attributed_reactions():
    head = "На 28 октября 2026 года готовность объекта составляет 37%"
    text = head + ", что усиливает сомнения в новом сроке."
    row = {**fact("Готовность объекта достигла 37% и вызвала сомнения дольщиков."),
           "kind": "fact", "date": "2026-10-28"}
    assert ground_observation_copy(text, [row], repair_readiness=True) == text
    row["verbatim"] = "Готовность объекта достигла 37%."
    attributed = (head + ". Ранее, 30 января 2026 года, по данным РНРФ, дольщики "
                  "выражали сомнения в соблюдении нового срока.")
    assert ground_observation_copy(attributed, [row], repair_readiness=True) == attributed
    undated = {**row, "date": None}
    separate_date = fact("Первая публикация вышла 28 октября 2026 года.")
    assert ground_observation_copy(text, [undated, separate_date], repair_readiness=True) == text
    negated = {**row, "verbatim": "Готовность объекта не достигла 37%."}
    assert ground_observation_copy(text, [negated], repair_readiness=True) == text


def test_future_configured_scenario_and_verbatim_quote_are_not_author_claims():
    claim = "Дольщики получили изменённые условия по ДДУ. Инициативная группа уже действует."
    assert editorial_claim_issues({
        "scenario": {"levels": [{"risks": [{"text": claim}]}]},
        "situation": {"buyers": {"quotes": [{"text": claim}]}},
    }, []) == []
