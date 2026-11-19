import pytest

from radar.reports.serious_claims import (
    asserts_filing_heading,
    authority_claim_issues,
    is_attributed_filing,
    normalize_forum_report_voice,
    serious_terms,
    without_reported_filing_authority_names,
)
from radar.reports.v8 import RULES


@pytest.mark.parametrize("text", [
    "Впоследствии срок был перенесён", "Последствия переноса сроков",
    "По последствию оценим работу", "Судя по фотографиям, работы ведутся",
    "Неизвестна судьба объекта", "Нужно рассудить участников",
])
def test_ordinary_chronology_and_judgment_are_not_legal_claims(text):
    assert serious_terms(text, RULES["needs_official_or_media"]) == set()


@pytest.mark.parametrize("text,term", [
    ("Следственный комитет", "следств"), ("Следствие установило", "следств"),
    ("Обратились в суд", "суд"), ("Решение суда", "суд"), ("В судах", "суд"),
    ("Судебное дело", "суд"), ("Судопроизводство", "суд"), ("Судимость", "суд"),
    ("Банкротство структуры", "банкрот"), ("Уголовное дело", "уголовн"),
    ("Незаконное строительство", "незаконн"), ("Строительство без разрешения", "без разрешения"),
])
def test_actual_legal_claims_still_require_their_evidence(text, term):
    assert term in serious_terms(text, RULES["needs_official_or_media"])


@pytest.fixture
def authority_evidence():
    return (
        {
            "facts": [
                {
                    "source_n": 1,
                    "date": "2026-02-01",
                    "verbatim": (
                        "Дольщики коллективно обратились в прокуратуру и Следственный комитет."
                    ),
                },
                {
                    "source_n": 2,
                    "date": "2026-02-05",
                    "verbatim": "«Эталон» объяснил перенос срока геологическими условиями.",
                },
                {
                    "source_n": 3,
                    "date": "2026-02-07",
                    "verbatim": "Следственный комитет зарегистрировал обращение дольщиков.",
                },
            ],
        },
        [
            {"n": 1, "contour": "reviews", "kind": "page"},
            {"n": 2, "contour": "media", "kind": "page"},
            {"n": 3, "contour": "authorities", "kind": "page"},
        ],
    )


@pytest.mark.parametrize(
    "copy,refs,blocked",
    [
        ("Дольщики обратились в прокуратуру и СК.", [1, 2], True),
        ("Дольщики обратились в прокуратуру и СК.", [2, 3], True),
        ("В феврале на форуме сообщили о коллективном обращении в прокуратуру и СК.", [1], False),
        ("На форуме сообщили об обращении в полицию.", [1], True),
        ("На форуме сообщили об обращении в СК. Ведомство получило жалобу.", [1], True),
        ("На форуме сообщили об обращении в СК, но ведомство получило жалобу.", [1], True),
        ("СК зарегистрировал обращение дольщиков.", [1, 3], False),
        ("СК зарегистрировал обращение дольщиков.", [1, 2], True),
        ("Ведомства получают картину из жалоб — позиция «Эталона» в этих каналах не представлена.",
         [1, 2], True),
        ("Позиция компании в ведомствах не представлена.", [2], True),
        ("Готовим типовые ответы на возможные запросы прокуратуры.", [], False),
        ("Если ведомство получит жалобу, готовим материалы для ответа.", [], False),
        ("До получения новых обращений готовим справку для ведомств.", [], False),
        ("Статус обращений в прокуратуру подлежит уточнению.", [], False),
    ],
)
def test_authority_claim_gate_matches_evidence_of_the_same_action(
    authority_evidence, copy, refs, blocked
):
    facts, sources = authority_evidence
    issues = authority_claim_issues(
        {"offer": {"modules": [{"pain": copy, "pain_refs": refs}]}}, facts, sources
    )
    if blocked:
        assert len(issues) == 1
        assert issues[0]["path"] == "offer.modules.0.pain"
        assert issues[0]["refs"] == refs
        assert issues[0]["kind"] == "authority_claim"
    else:
        assert issues == []


@pytest.mark.parametrize("kind,dated,blocked", [
    ("page", True, False), ("snippet", True, True), ("page", False, True),
])
def test_dated_original_publisher_can_confirm_filing(authority_evidence, kind, dated, blocked):
    facts, sources = authority_evidence
    sources[0].update(contour="media", kind=kind)
    facts["facts"][0]["date"] = "2026-02-01" if dated else None
    issues = authority_claim_issues(
        {"situation": {"lead": "Дольщики обратились в прокуратуру и СК."}}, facts, sources
    )
    assert bool(issues) is blocked


@pytest.mark.parametrize("quote,assertion", [
    ("Ведомство не получило жалобу дольщиков.", "Ведомство получило жалобу дольщиков."),
    ("Дольщики не направили жалобу в прокуратуру.", "Дольщики направили жалобу в прокуратуру."),
])
def test_denial_does_not_prove_positive_legal_assertion(authority_evidence, quote, assertion):
    facts, sources = authority_evidence
    facts["facts"][2]["verbatim"] = quote
    issues = authority_claim_issues(
        {"offer": {"modules": [{"pain": assertion, "pain_refs": [3]}]}}, facts, sources
    )
    assert [issue["path"] for issue in issues] == ["offer.modules.0.pain"]


def test_explicit_developer_recipient_is_not_a_legal_filing(authority_evidence):
    facts, sources = authority_evidence
    blocks = {"offer": {"modules": [{
        "name": "Материалы для надзора",
        "pain": "Дольщики направили жалобу застройщику.",
        "pain_refs": [1],
    }]}}
    assert authority_claim_issues(blocks, facts, sources) == []


def test_nagatino_rendered_offer_does_not_promote_owners_to_an_initiative_group(
    authority_evidence,
):
    facts, sources = authority_evidence
    facts["facts"][0]["verbatim"] = (
        "Пытаясь решить ситуацию, собственники коллективно обратились "
        "в прокуратуру, Мосгосстройнадзор и Следственный комитет."
    )
    module = {
        "pain": (
            "По сообщению собственников (источник 1), инициативная группа коллективно "
            "обратилась в прокуратуру, Мосгосстройнадзор и Следственный комитет. "
            "Агентство готовит справку для будущих запросов ведомств."
        ),
        "pain_refs": [1, 2],
    }
    blocks = {"offer": {"modules": [{}, {}, module]}}
    [issue] = authority_claim_issues(blocks, facts, sources)
    assert issue["path"] == "offer.modules.2.pain"
    assert issue["refs"] == [1, 2]
    assert "инициативной группе" in issue["reason"]
    module["pain"] = (
        "На форуме сообщили, что собственники коллективно обратились "
        "в прокуратуру, Мосгосстройнадзор и Следственный комитет."
    )
    assert authority_claim_issues(blocks, facts, sources) == []


def test_forum_repost_is_not_a_direct_statement_by_owners(authority_evidence):
    facts, sources = authority_evidence
    sources[0]["publisher"] = "pronovostroy.ru"
    facts["facts"][0]["verbatim"] = (
        "Пытаясь решить ситуацию, собственники коллективно обратились "
        "в прокуратуру, Мосгосстройнадзор и Следственный комитет."
    )
    path = "offer.modules.0.pain"
    module = {"pain_refs": [1, 2]}
    blocks = {"offer": {"modules": [module]}}
    for attribution in (
        "По сообщению собственников (источник 1), они коллективно обратились",
        "После обсуждения на форуме собственники коллективно обратились",
    ):
        module["pain"] = attribution + " в прокуратуру, Мосгосстройнадзор и СК."
        [issue] = authority_claim_issues(blocks, facts, sources)
        assert issue["path"] == path
        assert "подтверждено" in issue["reason"] or "Назови канал" in issue["reason"]
    for attribution in (
        "На форуме сообщили, что собственники коллективно обратились",
        "На форуме pronovostroy.ru сообщили, что собственники коллективно обратились",
        "По данным pronovostroy.ru, собственники коллективно обратились",
    ):
        module["pain"] = attribution + " в прокуратуру, Мосгосстройнадзор и СК."
        assert authority_claim_issues(blocks, facts, sources) == []


def test_forum_voice_restores_third_person_attribution_from_report_286(authority_evidence):
    facts, sources = authority_evidence
    sources[0]["url"] = "https://pronovostroy.ru/topic/25111/page-172"
    facts["facts"][0]["verbatim"] = (
        "Собственники коллективно обратились в прокуратуру и Следственный комитет."
    )
    claim = (
        "На форуме pronovostroy.ru собственники сообщили о коллективных обращениях "
        "в надзорные органы; новый срок они считают нереалистичным."
    )
    expected = (
        "На форуме pronovostroy.ru сообщили о коллективных обращениях собственников "
        "в надзорные органы; новый срок они считают нереалистичным."
    )
    assert normalize_forum_report_voice(claim, facts, sources, [1, 2]) == expected
    assert normalize_forum_report_voice(expected, facts, sources, [1]) == expected
    assert normalize_forum_report_voice(claim, facts, sources, [2]) == claim


def test_forum_voice_requires_matching_host_actor_and_recipient(authority_evidence):
    facts, sources = authority_evidence
    sources[0]["publisher"] = "pronovostroy.ru"
    for claim in (
        "На форуме example.org дольщики сообщили о коллективных обращениях в СК.",
        "На форуме pronovostroy.ru собственники сообщили о коллективных обращениях в СК.",
        "На форуме pronovostroy.ru дольщики сообщили о коллективных обращениях в полицию.",
        "На форуме pronovostroy.ru дольщики сообщили об обращении в СК и о том, "
        "что комитет принял жалобу.",
    ):
        assert normalize_forum_report_voice(claim, facts, sources, [1]) == claim


def test_forum_voice_does_not_override_a_matching_first_person_post(authority_evidence):
    facts, sources = authority_evidence
    sources[0]["publisher"] = "pronovostroy.ru"
    claim = (
        "В феврале 2026 года на форуме pronovostroy.ru дольщики сообщили "
        "о коллективном обращении в СК."
    )
    expected = (
        "В феврале 2026 года на форуме pronovostroy.ru сообщили "
        "о коллективном обращении дольщиков в СК."
    )
    facts["facts"].append({
        "source_n": 1,
        "date": "2026-02-01",
        "verbatim": "Мы коллективно обратились в Следственный комитет.",
    })
    assert normalize_forum_report_voice(claim, facts, sources, [1]) == claim
    facts["facts"].pop()
    assert normalize_forum_report_voice(claim, facts, sources, [1]) == expected


@pytest.mark.parametrize("date_phrase", ["", "в феврале 2026 г. ", "в феврале 2026г. "])
def test_named_forum_attribution_allows_exact_linked_filing(authority_evidence, date_phrase):
    facts, sources = authority_evidence
    facts["facts"][0]["verbatim"] = (
        "Собственники коллективно обратились в прокуратуру, Мосгосстройнадзор "
        "и Следственный комитет."
    )
    text = (
        f"На форуме pronovostroy.ru {date_phrase}сообщили о коллективных обращениях "
        "в прокуратуру, Мосгосстройнадзор и Следственный комитет."
    )
    blocks = {"situation": {"timeline": [{"text": text, "refs": [1]}]}}
    assert authority_claim_issues(blocks, facts, sources) == []
    masked = without_reported_filing_authority_names(text)
    assert len(masked) == len(text)
    assert "прокуратуру" in masked
    assert serious_terms(masked, RULES["needs_official_or_media"]) == set()


def test_clause_after_year_abbreviation_keeps_receipt_separate(authority_evidence):
    facts, sources = authority_evidence
    text = (
        "На форуме сообщили об обращении в Следственный комитет в 2026 г. "
        "Следственный комитет принял обращение."
    )
    blocks = {"situation": {"timeline": [{"text": text, "refs": [1]}]}}
    [issue] = authority_claim_issues(blocks, facts, sources)
    assert "получении или регистрации" in issue["reason"]
    masked = without_reported_filing_authority_names(text)
    assert masked.endswith("Следственный комитет принял обращение.")
    assert masked.count("Следственный комитет") == 1


@pytest.mark.parametrize("text,expected", [
    ("На форуме pronovostroy.ru сообщили об обращении дольщиков в ведомства.", True),
    ("По данным pronovostroy.ru, собственники коллективно обратились в СК.", True),
    ("По сообщению собственников, они обратились в надзорные органы.", False),
    ("На форуме сообщили о намерении направить жалобу в прокуратуру.", False),
    ("В ведомство направлена жалоба. На форуме обсуждаются сроки.", False),
])
def test_filing_body_requires_explicit_channel_in_same_clause(text, expected):
    assert is_attributed_filing(text) == expected


@pytest.mark.parametrize("heading,expected", [
    ("Коллективные обращения поданы", True),
    ("Собственники сообщили об обращениях в ведомства", True),
    ("Сообщения об обращениях", False),
    ("Сообщения об обращениях в прокуратуру", False),
    ("На форуме сообщили об обращении в СК", False),
    ("Обращения поданы застройщику", False),
    ("Подготовка справки для ведомства", False),
])
def test_filing_heading_distinguishes_assertion_from_neutral_topic(heading, expected):
    assert asserts_filing_heading(heading) == expected


def test_neutral_title_can_name_the_recipient_without_asserting_filing(authority_evidence):
    facts, sources = authority_evidence
    why_now = {
        "title": "Сообщения об обращениях в надзор",
        "text": "На форуме сообщили об обращении дольщиков в прокуратуру и СК.",
    }
    blocks = {"offer": {"why_now": [why_now]}}
    assert authority_claim_issues(blocks, facts, sources) == []
    for assertion in (
        "Собственники сообщили об обращениях в надзор",
        "Сообщения об обращениях в надзор. Ведомство возбудило дело",
    ):
        why_now["title"] = assertion
        assert [issue["path"] for issue in authority_claim_issues(blocks, facts, sources)] == [
            "offer.why_now.0.title",
        ]


@pytest.mark.parametrize("text", [
    "Следственный комитет возбудил уголовное дело.",
    "Арест подтверждён Следственным комитетом.",
    "Дольщики сообщили об обращении в Следственный комитет.",
    "На форуме сообщили, что жалобу принял Следственный комитет.",
    "На форуме сообщили об обращении в Следственный комитет и о том, что он расследует дело.",
])
def test_only_attributed_filing_masks_an_agency_name(text):
    assert without_reported_filing_authority_names(text) == text


def test_separate_legal_proceedings_keep_terms_after_attributed_filing():
    text = (
        "На форуме pronovostroy.ru сообщили об обращении дольщиков в Следственный комитет. "
        "В Следственном комитете начали проверку. Есть сообщение об аресте."
    )
    masked = without_reported_filing_authority_names(text)
    assert "в Следственный комитет" not in masked
    assert masked.endswith("В Следственном комитете начали проверку. Есть сообщение об аресте.")
    assert serious_terms(masked, RULES["needs_official_or_media"]) == {"следств", "арест"}


@pytest.mark.parametrize("source_kind,dated,blocked", [
    ("page", True, False), ("repost", True, True), ("page", False, True),
])
def test_attributed_first_person_account_is_distinct_from_a_repost(
    authority_evidence, source_kind, dated, blocked
):
    facts, sources = authority_evidence
    sources[0]["source_kind"] = source_kind
    facts["facts"][0].update(
        date="2026-02-01" if dated else None,
        verbatim="Мы, собственники дома, направили коллективную жалобу в СК. Анна, собственник.",
    )
    block = {"diagnosis": {"stage": {"signs": [{
        "text": "По словам собственников, они направили коллективную жалобу в СК.",
        "refs": [1],
    }]}}}
    issues = authority_claim_issues(block, facts, sources)
    assert bool(issues) is blocked


@pytest.mark.parametrize("actor,verb", [
    ("Инициативная группа дольщиков", "обратилась"),
    ("Представитель дольщиков", "обратился"),
])
def test_committed_roles_must_appear_in_the_filing_atom(authority_evidence, actor, verb):
    facts, sources = authority_evidence
    module = {
        "pain": f"По сообщению форума, {actor.casefold()} коллективно {verb} в СК.",
        "pain_refs": [1],
    }
    blocks = {"offer": {"modules": [module]}}
    facts["facts"][0]["verbatim"] += f" {actor} также участвовали в обсуждении."
    assert [issue["path"] for issue in authority_claim_issues(blocks, facts, sources)] == [
        "offer.modules.0.pain",
    ]
    facts["facts"][0]["verbatim"] = f"{actor} коллективно {verb} в Следственный комитет."
    assert authority_claim_issues(blocks, facts, sources) == []


def test_finding_representatives_remains_future_agency_work(authority_evidence):
    facts, sources = authority_evidence
    module = {
        "pain": "По данным форума, дольщики обратились в СК и ищут представителя дольщиков.",
        "pain_refs": [1],
        "solution": "Ищем добровольных представителей для переговоров с застройщиком.",
    }
    assert authority_claim_issues({"offer": {"modules": [module]}}, facts, sources) == []


def test_prospective_deliverable_does_not_precede_the_same_past_complaint(authority_evidence):
    facts, sources = authority_evidence
    module = {
        "pain": "На форуме сообщили об обращении в СК.",
        "pain_refs": [1],
        "outcome": "Ведомства получают позицию застройщика до или одновременно с жалобами.",
    }
    assert [i["path"] for i in authority_claim_issues(
        {"offer": {"modules": [module]}}, facts, sources
    )] == ["offer.modules.0.outcome"]
    module["outcome"] = "Готовая справка помогает отвечать на будущие запросы ведомств."
    assert authority_claim_issues({"offer": {"modules": [module]}}, facts, sources) == []


@pytest.mark.parametrize("outcome,blocked", [
    ("Ведомства получают позицию застройщика до рассмотрения жалоб.", True),
    ("Справку передадут в прокуратуру к началу рассмотрения новых обращений.", True),
    ("Согласованная справка готова для возможных запросов ведомств.", False),
    ("Агентство готовит справку до рассмотрения новых жалоб командой застройщика.", False),
])
def test_prospective_deliverable_does_not_promise_agency_review_timing(
    authority_evidence, outcome, blocked
):
    facts, sources = authority_evidence
    module = {"pain_refs": [1], "outcome": outcome}
    issues = authority_claim_issues({"offer": {"modules": [module]}}, facts, sources)
    assert [issue["path"] for issue in issues] == (["offer.modules.0.outcome"] if blocked else [])


def test_work_plan_does_not_anticipate_a_reported_filing(authority_evidence):
    facts, sources = authority_evidence
    module = {
        "pain_refs": [1], "outcome": "Подготовлен согласованный ответ",
        "solution": "Вручаем ответ заявителям до отправки в надзор.",
    }
    blocks = {"offer": {"modules": [module]}}
    assert [issue["path"] for issue in authority_claim_issues(blocks, facts, sources)] == [
        "offer.modules.0.solution",
    ]
    module["solution"] = "Передаём ответ заявителям и готовим справку для будущих запросов."
    assert authority_claim_issues(blocks, facts, sources) == []


def test_generic_scenario_description_does_not_assert_a_case(authority_evidence):
    facts, sources = authority_evidence
    blocks = {"scenario": {"levels": [{"risks": [{
        "text": "Ведомство получило жалобы и поставило объект на контроль.",
    }]}]}}
    assert authority_claim_issues(blocks, facts, sources) == []
