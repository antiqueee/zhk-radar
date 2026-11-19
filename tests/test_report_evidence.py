from datetime import UTC, date, datetime

import pytest

from radar.web.report_evidence import (
    ResearchEvidence,
    as_of_date,
    date_span,
    fresh,
    internal_priority,
    monitoring_scale,
    normalize_research,
    normalize_sources,
    qualify,
    research_context,
    source_contour,
)


@pytest.fixture
def materials():
    sources = [
        {
            "n": 1,
            "url": "https://наш.дом.рф/card/1",
            "published_at": "2026-11-01",
            "text": (
                "Срок перенесён; идёт строительство. Срок ввода 31.03.2027. "
                "Застройщик Тест продолжает строительство объекта."
            ),
            "type": "карточка объекта",
        },
        {
            "n": 2,
            "url": "https://msk1.ru/story/2",
            "published_at": "2026-11-02",
            "text": "Дольщики обсуждают перенос",
            "type": "публикация",
        },
        {
            "n": 3,
            "url": "https://cian.ru/reviews/3",
            "published_at": "2026-11-03",
            "text": "Дольщики ждут прямого ответа",
            "type": "отзывы",
        },
        {
            "n": 4,
            "url": "https://t.me/example/4",
            "published_at": "2026-11-04",
            "text": "Обсуждают коллективное обращение",
            "kind": "chat",
            "type": "чат",
        },
    ]
    facts = [
        {
            "text": s["text"],
            "date": s["published_at"],
            "source_n": s["n"],
            "who_says": "источник",
            "kind": "claim",
            "verbatim": s["text"],
        }
        for s in sources
    ]
    research = {
        "passport": {
            "official_name": "ЖК Тест",
            "aliases": ["Тест"],
            "developer_brand": "Застройщик Тест",
            "developer_legal": "ООО Тест",
            "developer_refs": [1],
            "tech_customer": None,
            "address": "Москва",
            "official_card_url": sources[0]["url"],
            "phases": [
                {
                    "name": "Корпус 1",
                    "scope": "residential",
                    "completion_date": "2027-03-31",
                    "keys_date": None,
                    "refs": [1],
                }
            ],
            "status": "under_construction",
            "status_date": "2026-11-01",
            "status_refs": [1],
            "developer_status": "active",
            "developer_status_date": "2026-11-01",
            "developer_status_refs": [1],
            "addressee": "developer_active",
            "addressee_refs": [1],
            "communities": [sources[3]["url"]],
        },
        "evidence": [
            {
                "id": f"e{n}",
                "source_n": n,
                "date_iso": f"2026-11-0{n}",
                "date_kind": "message" if n == 4 else "review" if n == 3 else "publication",
                "problem_id": "delay" if n <= 2 else "answer",
                "fact_indexes": [n - 1],
                "in_scope": True,
            }
            for n in range(1, 5)
        ],
        "problems": [
            {
                "id": "delay",
                "name": "Перенос срока",
                "topic": "deadlines",
                "in_scope": True,
                "status": "active",
                "evidence_ids": ["e1", "e2"],
                "corpuses": ["Корпус 1"],
            },
            {
                "id": "answer",
                "name": "Нет прямого ответа",
                "topic": "communication",
                "in_scope": True,
                "status": "active",
                "evidence_ids": ["e3", "e4"],
                "corpuses": ["Корпус 1"],
            },
        ],
        "stage_signs": [
            {
                "stage": 2,
                "text": "Подготовка коллективного обращения",
                "date_iso": "2026-11-04",
                "source_n": 4,
                "fact_indexes": [3],
            }
        ],
        "manual_checks": [],
    }
    return research, sources, facts


def test_full_qualification_is_based_on_counts_and_grounded_stage(materials):
    research, sources, facts = materials
    result = qualify(research, sources, facts, "2026-11-16")
    assert result["verdict"] == "report_full"
    assert result["client_role"] == "застройщик"
    assert result["current_stage"] == 2
    assert result["coverage"]["dated_evidence_12m"] == 4
    assert result["coverage"]["problems_in_scope"] == 2
    assert result["coverage"]["chats_read"] is True
    assert result["coverage"]["official_deadline_found"] is True


def test_compact_requires_two_dated_witnesses(materials):
    research, sources, facts = materials
    research["evidence"] = research["evidence"][:2]
    research["problems"] = research["problems"][:1]
    result = qualify(research, sources, facts, "2026-11-16")
    assert result["verdict"] == "report_compact"
    assert result["coverage"]["problems_in_scope"] == 1


def test_one_publication_does_not_become_four_witnesses(materials):
    research, sources, facts = materials
    for item in research["evidence"]:
        item.update(source_n=1, date_iso="2026-11-01", fact_indexes=[0])
    result = qualify(research, sources, facts, "2026-11-16")
    assert result["coverage"]["dated_evidence_12m"] == 1
    assert result["coverage"]["problems_in_scope"] == 2
    assert result["verdict"] == "watch"


def test_checked_date_and_old_complaints_do_not_qualify(materials):
    research, sources, facts = materials
    research["evidence"][0]["date_kind"] = "checked"
    for item in research["evidence"][1:]:
        item["date_iso"] = "2024-01-01"
        facts[item["fact_indexes"][0]]["date"] = "2024-01-01"
    result = qualify(research, sources, facts, "2026-11-16")
    assert result["coverage"]["dated_evidence_12m"] == 0
    assert result["verdict"] == "watch"
    assert result["priority"] is None


def test_absence_of_stage_is_not_information_vacuum(materials):
    research, sources, facts = materials
    research["stage_signs"] = []
    result = qualify(research, sources, facts, "2026-11-16")
    assert result["verdict"] == "watch"
    assert result["current_stage"] is None
    assert "признаки" in " ".join(result["manual_checks"])


def test_old_stage_is_history_not_current_escalation(materials):
    research, sources, facts = materials
    research["stage_signs"][0].update(stage=4, date_iso="2024-01-01")
    facts[3]["date"] = "2024-01-01"
    result = qualify(research, sources, facts, "2026-11-16")
    assert result["current_stage"] is None
    assert result["verdict"] == "watch"


def test_unknown_developer_is_hold_not_fiction(materials):
    research, sources, facts = materials
    research["passport"]["developer_brand"] = "не установлен"
    result = qualify(research, sources, facts, "2026-11-16")
    assert result["verdict"] == "hold"
    assert "developer_unknown" in result["reasons"]
    assert result["bot_message"].endswith("юрлицу")


def test_delivered_historical_delay_requires_manual_decision(materials):
    research, sources, facts = materials
    research["passport"]["status"] = "keys_issued"
    facts[0]["text"] = sources[0]["text"] = (
        "Ключи выданы. Застройщик Тест — действующий застройщик. Срок ввода 31.03.2027."
    )
    facts[0]["verbatim"] = facts[0]["text"]
    result = qualify(research, sources, facts, "2026-11-16")
    assert result["verdict"] == "hold"
    assert "object_delivered" in result["reasons"]


def test_current_acceptance_quality_can_still_concern_developer(materials):
    research, sources, facts = materials
    research["passport"]["status"] = "keys_issued"
    facts[0]["text"] = sources[0]["text"] = (
        "Ключи выданы. Застройщик Тест — действующий застройщик. Срок ввода 31.03.2027."
    )
    facts[0]["verbatim"] = facts[0]["text"]
    research["problems"][0].update(topic="acceptance", name="Дефекты при приёмке")
    assert qualify(research, sources, facts, "2026-11-16")["verdict"] == "report_full"


def test_bankrupt_old_developer_can_have_tech_customer_as_client(materials):
    research, sources, facts = materials
    research["passport"].update(
        developer_status="bankruptcy", addressee="tech_customer", tech_customer="Техзаказчик Тест"
    )
    facts[0]["text"] = sources[0]["text"] = (
        "Застройщик Тест признан банкротом. Технический заказчик Техзаказчик Тест "
        "достраивает объект, строительство продолжается. Срок ввода 31.03.2027."
    )
    facts[0]["verbatim"] = facts[0]["text"]
    result = qualify(research, sources, facts, "2026-11-16")
    assert result["verdict"] == "report_full"
    assert result["client_role"] == "заказчик"
    research["passport"].update(addressee="none", addressee_refs=[])
    result = qualify(research, sources, facts, "2026-11-16")
    assert result["verdict"] == "hold"
    assert "no_solvent_addressee" in result["reasons"]


def test_fund_and_city_are_manual_decisions(materials):
    research, sources, facts = materials
    research["passport"]["addressee"] = "fund_or_city"
    facts[0]["text"] = sources[0]["text"] = (
        "Город достраивает объект, строительство продолжается. Срок ввода 31.03.2027."
    )
    facts[0]["verbatim"] = facts[0]["text"]
    result = qualify(research, sources, facts, "2026-11-16")
    assert result["verdict"] == "hold"
    assert "fund_or_city" in result["reasons"]


def test_stale_status_is_not_treated_as_current(materials):
    research, sources, facts = materials
    research["passport"]["status_date"] = "2024-01-01"
    facts.append({"date": "2024-01-01", "source_n": 1})
    result = qualify(research, sources, facts, "2026-11-16")
    assert result["passport"]["status"] == "unknown"
    assert result["verdict"] == "hold"
    assert "current_status_or_addressee_unverified" in result["reasons"]


def test_unrelated_or_invented_date_is_rejected(materials):
    research, sources, facts = materials
    research["evidence"][0]["date_iso"] = "2026-11-15"
    with pytest.raises(ValueError, match="Дата свидетельства"):
        normalize_research(research, {}, sources, facts, "2026-11-16")


def test_invented_phase_deadline_cannot_raise_urgency(materials):
    research, sources, facts = materials
    research["passport"]["phases"][0]["completion_date"] = "2027-04-30"
    with pytest.raises(ValueError, match="Срок корпуса"):
        normalize_research(research, {}, sources, facts, "2026-11-16")


def test_unknown_ref_or_other_source_fact_is_rejected(materials):
    research, sources, facts = materials
    research["evidence"][0]["source_n"] = 99
    with pytest.raises(ValueError, match="Связь исследования"):
        normalize_research(research, {}, sources, facts)
    research["evidence"][0]["source_n"] = 2
    with pytest.raises(ValueError, match="другого источника"):
        normalize_research(research, {}, sources, facts)


def test_month_precision_and_calendar_cutoff_are_preserved(materials):
    research, sources, facts = materials
    research["evidence"][0]["date_iso"] = "2026-11"
    facts[0]["date"] = "ноябрь 2026 года"
    assert normalize_research(research, {}, sources, facts, "2026-11-16")
    research["evidence"][0]["date_iso"] = "2026-11-01"
    sources[0]["published_at"] = None
    with pytest.raises(ValueError, match="Дата свидетельства"):
        normalize_research(research, {}, sources, facts, "2026-11-16")
    assert fresh("2026-11", "2026-11-16")
    assert not fresh("2025", "2026-11-16")
    assert not fresh("2025-11", "2026-11-16")
    assert not fresh("2026-12-01", "2026-11-16")
    assert fresh("2023-02-28", "2024-02-29")


@pytest.mark.parametrize(
    "value,start,end",
    [
        ("II кв. 2027", date(2027, 4, 1), date(2027, 6, 30)),
        ("2 квартал 2027 года", date(2027, 4, 1), date(2027, 6, 30)),
        ("IV квартал 2026", date(2026, 10, 1), date(2026, 12, 31)),
        ("12 декабря 2024", date(2024, 12, 12), date(2024, 12, 12)),
        ("14 ноября 2026 года", date(2026, 11, 14), date(2026, 11, 14)),
    ],
)
def test_explicit_russian_day_and_quarter_keep_their_precision(value, start, end):
    assert date_span(value) == (start, end)


def test_invalid_russian_date_does_not_fall_back_to_a_month():
    assert date_span("31 февраля 2024") is None
    assert date_span("V кв. 2027") is None
    assert date_span("II кв.") is None
    assert date_span("проверено 12 декабря 2024") is None
    assert date_span("декабрь 2024") == (date(2024, 12, 1), date(2024, 12, 31))
    assert date_span("II кв. 2027") != date_span("2027-06-30")


def test_russian_fact_dates_ground_exact_event_and_quarter_without_substitution(materials):
    research, sources, facts = materials
    facts[0]["date"] = "12 декабря 2024"
    research["evidence"][0]["date_iso"] = "2024-12-12"
    research["passport"]["phases"][0]["completion_date"] = "2027-Q2"
    facts.append({"source_n": 1, "date": "II кв. 2027", "text": "Сдача во II квартале 2027"})
    value = normalize_research(research, {}, sources, facts, "2026-11-16")
    assert value.evidence[0].date_iso == "2024-12-12"
    assert value.passport.phases[0].completion_date == "2027-Q2"
    research["passport"]["phases"][0]["completion_date"] = "2027-06-30"
    with pytest.raises(ValueError, match="Срок корпуса"):
        normalize_research(research, {}, sources, facts, "2026-11-16")


def test_reviews_do_not_establish_bankruptcy_or_new_current_status(materials):
    research, sources, facts = materials
    research["passport"].update(
        developer_status="bankruptcy", developer_status_date="2026-11-03", developer_status_refs=[3]
    )
    value = normalize_research(research, {}, sources, facts, "2026-11-16")
    assert value.passport.developer_status == "unknown"


def test_publisher_date_and_chat_coverage_are_not_fabricated(materials):
    research, sources, facts = materials
    source = {
        "n": 1,
        "url": "https://xn--80az8a.xn--d1aqf.xn--p1ai/1",
        "text": "",
        "published_at": None,
    }
    value = normalize_sources([source], "2026-11-16")[0]
    assert value["publisher"] == "Наш.Дом.РФ"
    assert value["evidence_date"] is None
    assert value["display_date"] == "проверено 16.11.2026"
    sources[3]["text"] = ""
    assert not qualify(research, sources, facts, "2026-11-16")["coverage"]["chats_read"]


def test_context_priority_and_real_monitoring_rubric(materials):
    research, sources, facts = materials
    ctx = research_context(
        {"name": "ЖК Тест", "developer": "Тест"}, sources, {"facts": facts}, "2026-11-16"
    )
    assert ctx["as_of"] == "2026-11-16"
    assert ctx["facts"] == [{**fact, "index": i} for i, fact in enumerate(facts)]
    assert all("index" not in fact for fact in facts)  # Only model input is annotated.
    assert ctx["qualification_rules"]["verdicts"]["report_full"]["min_dated_evidence"] == 4
    assert ResearchEvidence.model_validate(research)
    priority = internal_priority(
        [{"name": "Острота", "score": 3}, {"name": "Публичность", "score": 2}]
    )
    assert priority == {"score": 5, "level": "high"}
    assert internal_priority([{"name": "Острота", "score": 3}] * 2) is None
    assert (
        internal_priority([{"name": "Острота", "score": 4}, {"name": "Публичность", "score": 2}])
        is None
    )
    rubric = monitoring_scale()
    assert rubric["max"] == 10
    assert "митинг" in rubric["anchors"][8]
    assert rubric["source"]["constant"] == "DEFAULT_WEEKLY_REPORT_RULES"
    assert as_of_date(datetime(2026, 11, 15, 22, tzinfo=UTC)).isoformat() == "2026-11-16"


@pytest.mark.parametrize("status", ["stalled", "keys_issued", "commissioned"])
def test_fresh_unrelated_publication_does_not_confirm_new_status(materials, status):
    research, sources, facts = materials
    research["passport"]["status"] = status
    value = normalize_research(research, {}, sources, facts, "2026-11-16")
    assert value.passport.status == "unknown"
    assert value.passport.status_fact_indexes == []


def test_unrelated_or_stale_fact_does_not_confirm_addressee(materials):
    research, sources, facts = materials
    research["passport"].update(addressee="tech_customer", tech_customer="Выдуманный Техзаказчик")
    value = normalize_research(research, {}, sources, facts, "2026-11-16")
    assert value.passport.addressee == "none"
    research["passport"].update(addressee="developer_active", addressee_fact_indexes=[0])
    facts[0]["date"] = "2024-01-01"
    value = normalize_research(research, {}, sources, facts, "2026-11-16")
    assert value.passport.addressee == "none"
    assert value.passport.status == "unknown"


def test_passport_fact_reference_cannot_borrow_another_source(materials):
    research, sources, facts = materials
    research["passport"]["status_fact_indexes"] = [2]
    with pytest.raises(ValueError, match="Факт паспорта"):
        normalize_research(research, {}, sources, facts, "2026-11-16")


def test_discard_mode_preserves_valid_evidence_without_reindexing(materials):
    research, sources, facts = materials
    research["evidence"][0]["fact_indexes"] = [2]  # Source3 cannot prove source1.
    research["stage_signs"].append({**research["stage_signs"][0], "source_n": 1})
    research["passport"]["addressee_fact_indexes"] = [2]
    issues = []
    value = normalize_research(
        research, {}, sources, facts, "2026-11-16", discard_invalid=True, issues=issues
    )
    assert [item.id for item in value.evidence] == ["e2", "e3", "e4"]
    assert value.evidence[0].fact_indexes == [1]
    assert value.problems[0].evidence_ids == ["e2"]
    assert len(value.stage_signs) == 1
    assert value.passport.addressee == "none"  # Not substituted with the otherwise-valid index0.
    assert any(item["path"] == "evidence/0" for item in issues)
    assert any(item["path"] == "stage_signs/1" for item in issues)
    assert value.manual_checks
    assert research["evidence"][0]["fact_indexes"] == [2]  # Input remains immutable.


def test_stage_sign_cannot_borrow_group_size_from_a_different_atom(materials):
    research, sources, facts = materials
    count = "658 дольщиков сомневаются в соблюдении срока передачи квартир."
    appeal = "Собственники коллективно направили обращение в стройнадзор."
    sources[3]["text"] = count + "\n" + appeal
    facts[3].update(text=count, verbatim=count)
    facts.append({**facts[3], "text": appeal, "verbatim": appeal})
    research["stage_signs"][0].update(
        stage=3,
        text="658 дольщиков коллективно направили обращение в стройнадзор.",
        fact_indexes=[3, 4],
    )
    with pytest.raises(ValueError, match="число и коллективное действие"):
        normalize_research(research, {}, sources, facts, "2026-11-16")
    issues = []
    value = normalize_research(
        research, {}, sources, facts, "2026-11-16", discard_invalid=True, issues=issues
    )
    assert value.stage_signs == [] and any(i["path"] == "stage_signs/0" for i in issues)
    assert qualify(value, sources, facts, "2026-11-16")["current_stage"] is None


def test_stage_sign_cannot_turn_geology_into_appeals_to_investigators(materials):
    research, sources, facts = materials
    geology = "Компания столкнулась с непредвиденными геологическими условиями."
    sources.append(
        {
            "n": 5,
            "url": "https://fontanka.ru/2026/11/05/news/",
            "text": geology,
            "published_at": "2026-11-05",
            "type": "публикация",
        }
    )
    facts.append(
        {
            "text": geology,
            "verbatim": geology,
            "date": "2026-11-05",
            "source_n": 5,
            "who_says": "Застройщик Тест",
            "kind": "fact",
        }
    )
    research["stage_signs"].append(
        {
            "stage": 3,
            "text": "Коллективные обращения направлены в Следственный комитет.",
            "source_n": 5,
            "date_iso": "2026-11-05",
            "fact_indexes": [4],
        }
    )
    with pytest.raises(ValueError, match="Событие признака стадии"):
        normalize_research(research, {}, sources, facts, "2026-11-16")
    issues = []
    value = normalize_research(
        research, {}, sources, facts, "2026-11-16", discard_invalid=True, issues=issues
    )
    assert len(value.stage_signs) == 1  # The genuinely grounded preparation sign remains unchanged.
    assert any(i["path"] == "stage_signs/1" for i in issues)
    assert qualify(value, sources, facts, "2026-11-16")["current_stage"] == 2


def test_real_collective_action_and_its_same_atom_count_can_confirm_stage(materials):
    research, sources, facts = materials
    text = "658 дольщиков направили коллективное обращение в стройнадзор."
    sources[3]["text"] = text
    facts[3].update(text=text, verbatim=text)
    research["stage_signs"][0].update(stage=3, text=text)
    value = normalize_research(research, {}, sources, facts, "2026-11-16")
    assert qualify(value, sources, facts, "2026-11-16")["current_stage"] == 3


@pytest.mark.parametrize(
    "source,action,should_keep",
    [
        ({"url": "https://pronovostroy.ru/topic/25111", "type": "отзывы"},
         "Собственники коллективно обратились в Следственный комитет.", False),
        ({"url": "https://fontanka.ru/story/5", "type": "публикация"},
         "Собственники направили коллективное обращение в стройнадзор.", True),
        ({"url": "https://www.mos.ru/news/5", "type": "ведомство"},
         "Начата проверка прокуратуры по обращению жильцов.", True),
        ({"url": "https://msk1.ru/story/5", "type": "публикация", "kind": "snippet"},
         "Собственники направили коллективное обращение в стройнадзор.", False),
        ({"url": "https://pronovostroy.ru/topic/25111", "type": "отзывы"},
         "Собственники направили коллективное обращение застройщику.", True),
    ],
)
def test_v8_legal_escalation_requires_dated_official_or_media_proof(
    materials, source, action, should_keep
):
    research, sources, facts = materials
    stamp = "2026-11-05"
    sources.append({"n": 5, "text": action, "published_at": stamp, **source})
    facts.append({
        "text": action, "verbatim": action, "date": stamp, "source_n": 5,
        "who_says": "Источник", "kind": "claim",
    })
    research["stage_signs"].append({
        "stage": 3, "text": action, "source_n": 5, "date_iso": stamp, "fact_indexes": [4],
    })

    previous = normalize_research(research, {}, sources, facts, "2026-11-16")
    assert qualify(previous, sources, facts, "2026-11-16")["current_stage"] == 3
    issues = []
    value = normalize_research(
        research, {}, sources, facts, "2026-11-16", discard_invalid=True, issues=issues,
        require_dated_escalation_source=True,
    )
    assert qualify(value, sources, facts, "2026-11-16")["current_stage"] == (
        3 if should_keep else 2
    )
    assert any(row["path"] == "stage_signs/1" and "датированного" in row["reason"]
               for row in issues) is not should_keep


def test_stage_cannot_treat_a_plan_to_appeal_as_an_already_sent_appeal(materials):
    research, sources, facts = materials
    research["stage_signs"][0].update(
        stage=3, text="Коллективное обращение уже направлено в стройнадзор."
    )
    with pytest.raises(ValueError, match="Событие признака стадии"):
        normalize_research(research, {}, sources, facts, "2026-11-16")


@pytest.mark.parametrize(
    "variant,expected",
    [("company", True), ("residents", False), ("no_comment", False), ("old", False)],
)
def test_company_position_in_dated_media_is_not_limited_to_official_sites(
    materials, variant, expected
):
    research, sources, facts = materials
    text = "В ЖК Тест застройщик Тест объяснил перенос непредвиденными геологическими условиями."
    if variant == "no_comment":
        text = "Застройщик Тест не прокомментировал перенос сроков."
    if variant == "residents":
        text = "Дольщики ЖК Тест считают причиной переноса нехватку финансирования."
    stamp = "2024-11-05" if variant == "old" else "2026-11-05"
    sources.append(
        {
            "n": 5,
            "url": "https://fontanka.ru/2026/11/05/news/",
            "text": text,
            "published_at": stamp,
            "type": "публикация",
        }
    )
    facts.append(
        {
            "text": text,
            "verbatim": text,
            "date": stamp,
            "source_n": 5,
            "who_says": "Дольщики" if variant == "residents" else "Застройщик Тест",
            "kind": "fact",
        }
    )
    value = qualify(research, sources, facts, "2026-11-16")
    assert value["coverage"]["developer_position_found"] is expected


@pytest.mark.parametrize(
    "quote,who",
    [
        ("Готовность ЖК Тест достигла 37%.", "Застройщик Тест"),
        ("В ЖК Тест мы провели исследования геологии.", "Президент чужой компании"),
        ("В ЖК Тест нам обещали передать квартиру к декабрю.", "Застройщик Тест"),
    ],
)
def test_model_attribution_alone_cannot_invent_company_position(materials, quote, who):
    research, sources, facts = materials
    sources.append(
        {
            "n": 5,
            "url": "https://fontanka.ru/2026/11/05/news/",
            "text": quote,
            "title": "ЖК Тест",
            "published_at": "2026-11-05",
            "type": "публикация",
        }
    )
    facts.append(
        {
            "text": quote,
            "verbatim": quote,
            "date": "2026-11-05",
            "source_n": 5,
            "who_says": who,
            "kind": "fact",
        }
    )
    assert not qualify(research, sources, facts, "2026-11-16")["coverage"][
        "developer_position_found"
    ]


def test_discard_mode_removes_wrong_problem_backlinks_not_other_problem_evidence(materials):
    research, sources, facts = materials
    research["problems"][0]["evidence_ids"].append("e4")
    value = normalize_research(research, {}, sources, facts, "2026-11-16", discard_invalid=True)
    assert value.problems[0].evidence_ids == ["e1", "e2"]
    assert value.problems[1].evidence_ids == ["e3", "e4"]
    assert len(value.evidence) == 4


def test_negated_or_future_addressee_is_not_confirmed(materials):
    research, sources, facts = materials
    research["passport"]["phases"] = []
    for text in (
        "Застройщик Тест не является застройщиком объекта.",
        "Застройщик Тест планирует строить объект.",
    ):
        facts[0]["text"] = facts[0]["verbatim"] = sources[0]["text"] = text
        assert (
            normalize_research(research, {}, sources, facts, "2026-11-16").passport.addressee
            == "none"
        )


def test_hallucinated_fact_summary_or_snippet_does_not_confirm_passport(materials):
    research, sources, facts = materials
    research["passport"]["status"] = "stalled"
    facts[0]["text"] = "Строительство остановлено"
    value = normalize_research(research, {}, sources, facts, "2026-11-16")
    assert value.passport.status == "unknown"  # Exact quotation says construction continues.
    research["passport"]["status"] = "under_construction"
    facts[0]["text"] = facts[0]["verbatim"]
    sources[0]["kind"] = "snippet"
    value = normalize_research(research, {}, sources, facts, "2026-11-16")
    assert value.passport.status == "unknown"
    assert value.passport.addressee == "none"


@pytest.mark.parametrize(
    "text,status",
    [
        ("Строительство возобновлено, ведутся отделочные работы.", "under_construction"),
        (
            "В настоящий момент активно выполняются монолитные работы выше нулевого уровня.",
            "under_construction",
        ),
        ("Работы на площадке приостановлены, рабочих нет.", "stalled"),
        ("Получено разрешение на ввод комплекса в эксплуатацию.", "commissioned"),
        ("Началась передача ключей дольщикам.", "keys_issued"),
    ],
)
def test_real_dated_status_wording_is_not_overblocked(materials, text, status):
    research, sources, facts = materials
    sources.append(
        {
            "n": 5,
            "url": "https://msk1.ru/story/5",
            "published_at": "2026-11-05",
            "text": text,
            "type": "публикация",
        }
    )
    facts.append({"text": text, "verbatim": text, "date": "2026-11-05", "source_n": 5})
    research["passport"].update(
        status=status, status_date="2026-11-05", status_refs=[5], status_fact_indexes=[4]
    )
    value = normalize_research(research, {}, sources, facts, "2026-11-16")
    assert value.passport.status == status
    assert value.passport.status_fact_indexes == [4]


@pytest.mark.parametrize(
    "text,status",
    [
        ("Ключи обещают выдать в следующем месяце.", "keys_issued"),
        ("Есть риск остановки строительства.", "stalled"),
        ("Монолитные работы не выполняются.", "under_construction"),
        ("Не выполняются монолитные работы.", "under_construction"),
        ("Будут выполняться монолитные работы выше нулевого уровня.", "under_construction"),
        ("Разрешение на ввод не получено.", "commissioned"),
    ],
)
def test_forecast_or_negated_status_is_not_confirmation(materials, text, status):
    research, sources, facts = materials
    facts[0]["text"] = facts[0]["verbatim"] = sources[0]["text"] = text
    research["passport"]["phases"] = []
    research["passport"]["status"] = status
    assert (
        normalize_research(research, {}, sources, facts, "2026-11-16").passport.status == "unknown"
    )


def test_unknown_domain_is_not_media_or_extra_contour(materials):
    research, sources, facts = materials
    for source in sources:
        source.update(
            url=f"https://forum-unknown.example/{source['n']}", contour="other", type="page"
        )
    value = qualify(research, sources, facts, "2026-11-16")
    assert value["verdict"] == "watch"
    assert value["coverage"]["contours"] == []
    assert value["coverage"]["active_contours"] == []
    assert value["coverage"]["dated_evidence_12m"] == 4
    assert all(p["level"] == "C" for p in value["problem_registry"])
    assert value["passport"]["status"] == "unknown"
    assert (
        normalize_sources([{"n": 1, "url": "https://forum.example/thread", "text": "Текст"}])[0][
            "contour"
        ]
        == "other"
    )
    source = {"n": 1, "url": "https://msk1.ru/1", "published_at": "2026-11-15T22:00:00Z"}
    assert normalize_sources([source], "2026-11-16")[0]["display_date"] == "16.11.2026"


@pytest.mark.parametrize(
    "host,path",
    [
        ("msk1.ru", "/text/realty/2026/10/27/76661830/comments/"),
        ("rbc.ru", "/story/comment/"),
    ],
)
def test_newsroom_reader_comments_are_reviews_even_with_media_metadata(host, path):
    source = {
        "n": 1,
        "url": f"https://{host}{path}",
        "kind": "page",
        "contour": "media",
        "type": "публикация",
        "text": "Читатель утверждает, что девелопер банкрот",
    }
    assert source_contour(source) == "reviews"
    normal = normalize_sources([source], "2026-11-16")[0]
    assert normal["contour"] == "reviews"
    assert normal["type"] == "отзывы"
    article = {**source, "url": f"https://{host}/text/realty/article/"}
    assert source_contour(article) == "media"
    assert normalize_sources([article], "2026-11-16")[0]["type"] == "публикация"


@pytest.mark.parametrize("dash", ["-", "–", "—"])
def test_explicit_news_title_suffix_supplies_publication_date(dash):
    source = {
        "n": 1,
        "url": "https://msk1.ru/text/realty/article/",
        "title": f"Дольщики рассказали о задержке {dash} 31 марта 2025 | МСК1.ру",
        "published_at": None,
    }
    result = normalize_sources([source], "2026-11-16")[0]
    assert result["published_at"] == result["evidence_date"] == "2025-03-31"
    assert result["display_date"] == "31.03.2025"
    assert result["publication_date_provenance"] == "title"
    assert not fresh(result["evidence_date"], "2026-11-16")
    assert normalize_sources([result], "2026-11-16")[0] == result


@pytest.mark.parametrize(
    "title",
    [
        "Срок ввода 31 марта 2025",
        "Срок ввода 31 марта 2025 | МСК1.ру",
        "Новость - 31 марта 2025",
        "Новость - 31 марта 2025 | ",
        "Новость - 31 марта 2025 | МСК1.ру | другое",
        "Новость - 31 февраля 2025 | МСК1.ру",
        "Новость - март 2025 | МСК1.ру",
    ],
)
def test_title_subject_url_or_incomplete_suffix_is_not_publication_date(title):
    source = {"n": 1, "url": "https://msk1.ru/2025/03/31/article", "title": title}
    result = normalize_sources([source], "2026-11-16")[0]
    assert result["evidence_date"] is None
    assert not result.get("publication_date_provenance")
    assert result["display_date"] == "проверено 16.11.2026"


def test_title_fallback_does_not_replace_metadata_or_utc_timestamp():
    source = {
        "n": 1,
        "url": "https://msk1.ru/article",
        "published_at": "2026-11-15T22:00:00Z",
        "title": "Историческая дата - 31 марта 2025 | МСК1.ру",
    }
    result = normalize_sources([source], "2026-11-16")[0]
    assert result["published_at"] == result["evidence_date"] == source["published_at"]
    assert result["display_date"] == "16.11.2026"
    assert result["publication_date_provenance"] == "metadata:published_at"


def test_future_news_title_date_is_preserved_but_does_not_prove_fresh_evidence():
    result = normalize_sources(
        [
            {
                "n": 1,
                "url": "https://msk1.ru/article",
                "title": "Публикация - 31 марта 2027 | МСК1.ру",
            }
        ],
        "2026-11-16",
    )[0]
    assert result["evidence_date"] == "2027-03-31"
    assert result["publication_date_provenance"] == "title"
    assert not fresh(result["evidence_date"], "2026-11-16")


def test_imported_chat_messages_and_real_summary_count_as_read(materials):
    import json

    research, sources, facts = materials
    sources[3]["url"] = None
    assert qualify(research, sources, facts, "2026-11-16")["coverage"]["chats_read"]
    sources[3].update(
        id="chat-summary:fixture",
        count=1,
        text=json.dumps({"messages_30d": 1, "quotes": [{"text": facts[3]["verbatim"]}]}),
    )
    assert qualify(research, sources, facts, "2026-11-16")["coverage"]["chats_read"]
    sources[3].update(count=0, text=json.dumps({"messages_30d": 0, "quotes": []}))
    assert not qualify(research, sources, facts, "2026-11-16")["coverage"]["chats_read"]


@pytest.mark.parametrize(
    "url,kind,read_status",
    [
        ("https://t.me/example", "page", "loaded"),
        ("https://t.me/example/4", "snippet", "unavailable"),
        ("https://t.me/example/4", "page", "unavailable"),
        ("https://t.me/example/4", None, "loaded"),
        ("https://youtube.com/watch?v=example", "page", "loaded"),
        ("https://vk.com/example", "page", "loaded"),
        ("https://t.me.evil.example/example/4", "page", "loaded"),
    ],
)
def test_community_landing_snippet_or_video_is_not_a_read_chat(materials, url, kind, read_status):
    research, sources, facts = materials
    sources[3].update(url=url, kind=kind, read_status=read_status, contour="communities")
    assert not qualify(research, sources, facts, "2026-11-16")["coverage"]["chats_read"]


@pytest.mark.parametrize("path", ["/example/4", "/s/example", "/s/example/4"])
def test_loaded_public_message_page_requires_dated_message_evidence(materials, path):
    research, sources, facts = materials
    sources[3].update(url="https://t.me" + path, kind="page", read_status="loaded")
    assert qualify(research, sources, facts, "2026-11-16")["coverage"]["chats_read"]
    research["evidence"][3]["date_kind"] = "publication"
    assert not qualify(research, sources, facts, "2026-11-16")["coverage"]["chats_read"]
    research["evidence"][3].update(date_kind="message", date_iso=None)
    assert not qualify(research, sources, facts, "2026-11-16")["coverage"]["chats_read"]


@pytest.mark.parametrize(
    "literal,wanted",
    [
        ("II кв. 2027", "2027-Q2"),
        ("2 квартале 2027 года", "2027-Q2"),
        ("12 декабря 2026 года", "2026-12-12"),
        ("апрелю 2027 года", "2027-04"),
    ],
)
def test_explicit_deadline_inside_publication_dated_fact_is_supported(literal, wanted):
    from radar.web.report_evidence import _supported_date

    sources = {1: {"published_at": "2026-11-16"}}
    facts = [{"source_n": 1, "date": "2026-11-16", "text": "Плановый ввод: " + literal}]
    assert _supported_date(wanted, [1], sources, facts)
    assert not _supported_date(wanted, [2], {2: sources[1]}, facts)


@pytest.mark.parametrize(
    "literal,wanted",
    [
        ("II кв. 2027", "2027-06-30"),
        ("II кв. 2027", "2027-04"),
        ("апрель 2027; корпус 1", "2027-04-01"),
        ("12 декабря 2026", "2026-12-13"),
    ],
)
def test_deadline_precision_is_never_invented_from_separate_numbers(literal, wanted):
    from radar.web.report_evidence import _supported_date

    assert not _supported_date(
        wanted,
        [1],
        {1: {"published_at": "2026-11-16"}},
        [{"source_n": 1, "date": "2026-11-16", "text": literal}],
    )


def test_quarter_phase_deadline_survives_normalization_with_publication_date(materials):
    research, sources, facts = materials
    research["passport"]["phases"][0]["completion_date"] = "2027-Q2"
    sources[0]["text"] += " Плановый ввод — II кв. 2027."
    facts.append(
        {
            "source_n": 1,
            "date": "2026-11-01",
            "text": "Плановый ввод — II кв. 2027.",
            "verbatim": "Плановый ввод — II кв. 2027.",
        }
    )
    normalized = normalize_research(research, {}, sources, facts, "2026-11-16")
    assert normalized.passport.phases[0].completion_date == "2027-Q2"


@pytest.mark.parametrize(
    "statement,expected",
    [
        ("КРОСТ ведёт строительство II очереди", True),
        ("КРОСТ ведет строительство II очереди", True),
        ("КРОСТ не ведёт строительство II очереди", False),
        ("КРОСТ больше не ведёт строительство II очереди", False),
        ("КРОСТ прекратил вести строительство II очереди", False),
        ("КРОСТ ведёт строительство, но работы приостановлены", False),
        ("КРОСТ ведёт строительство, введено конкурсное производство", False),
        ("КРОСТ планирует вести строительство II очереди", False),
    ],
)
def test_developer_active_construction_verb_respects_negation_and_stoppage(statement, expected):
    from radar.web.report_evidence import _status_meaning

    assert _status_meaning(statement, "active") is expected


@pytest.mark.parametrize(
    "observed,expected",
    [("2026-11-01", "active"), ("2025-01-01", "unknown"), ("2027-01-01", "unknown")],
)
def test_developer_construction_claim_still_requires_fresh_date(materials, observed, expected):
    research, sources, facts = materials
    statement = "Застройщик Тест ведёт строительство объекта."
    sources[0].update(text=statement, published_at=observed)
    facts[0].update(text=statement, verbatim=statement, date=observed)
    research["passport"]["phases"] = []
    research["passport"].update(
        status_date=observed,
        developer_status_date=observed,
        developer_status_fact_indexes=[0],
    )
    research["evidence"][0]["date_iso"] = observed
    normalized = normalize_research(research, {}, sources, facts, "2026-11-16")
    assert normalized.passport.developer_status == expected
