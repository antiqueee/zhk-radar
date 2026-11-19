"""Offline regressions for source loss and author-corrupted buyer quotations."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from radar.reports import pdf_evidence as evidence


def source(n, text, contour="media", *, url=None, date="2026-02-05"):
    return {"n": n, "text": text, "title": "Материал об объекте", "contour": contour,
            "url": url or f"https://source{n}.example/news", "publisher": f"Источник {n}",
            "evidence_date": date}


def fact(n, quote, *, kind="fact", who="Редакция", date="2026-02-05", text=None):
    return {"source_n": n, "text": text or quote, "verbatim": quote,
            "kind": kind, "who_says": who, "date": date}


@pytest.fixture
def nagatino():
    # Structural replay of brief255: two nested official cards, an old brand
    # launch story, current progress, incident, forum and the direct company reply.
    shared = " ".join(f"слово{i}" for i in range(120))
    card = "Срок сдачи II квартал 2027. Есть сданные корпуса."
    brand = "Nagatino I-Land"
    incident = "Застройщик перенёс ключи с марта 2026 на декабрь 2027."
    progress = "Сейчас выполняются монолитные работы в корпусах 1–3, готовность 37%."
    appeal = "На форуме опубликовано сообщение о коллективном обращении."
    position = (
        "В проекте Nagatino I-Land мы столкнулись с геологией и провели исследования."
    )
    buyer = "В моём договоре стоит декабрь 2026, позже срок перенесли."
    sources = [
        source(1, shared + " " + card + " " + brand, "official",
               url="https://developer.example/projects/island", date=None),
        source(2, appeal + " " + buyer, "reviews", date="2026-02-01"),
        source(3, progress, date="2026-10-28"),
        source(4, "Рекламное описание квартир и планировок без событий.", "reviews", date=None),
        source(5, shared + " " + brand, "official",
               url="https://developer.example/projects/island/flat", date=None),
        source(6, incident, date="2026-01-30"),
        source(7, "На сайте доступна камера стройки.", "official", date=None),
        source(8, position, date="2026-02-05"),
        source(9, "Красивый комплекс у реки представлен в обзоре.", "communities"),
        source(10, "Компания Девелопер начала проект много лет назад.", date="2020-07-09"),
        source(11, "У нас слабый напор воды в сданном корпусе.", "communities",
               date="2026-11-16"),
    ]
    facts = [
        fact(1, card, date=None), fact(2, appeal, kind="claim", date="2026-02-01"),
        fact(2, buyer, kind="claim", who="Дольщик", date="2026-03-16"),
        fact(3, progress, date="2026-10-28"), fact(4, sources[3]["text"], date=None),
        fact(5, brand, date=None), fact(6, incident, date="2026-01-30"),
        fact(7, sources[6]["text"], date=None),
        fact(8, position, who="Президент компании Девелопер"),
        fact(9, sources[8]["text"]), fact(10, sources[9]["text"], date="2020-07-09"),
        fact(11, sources[10]["text"], kind="claim", who="Собственник", date="2026-11-16"),
    ]
    qualification = {
        "verdict": "report_full", "current_stage": 3,
        "passport": {"official_name": "Nagatino I-Land", "developer_brand": "Девелопер",
                     "developer_refs": [10],
                     "status": "under_construction", "status_refs": [3],
                     "status_fact_indexes": [3], "addressee": "developer_active",
                     "addressee_refs": [6, 8], "addressee_fact_indexes": [6, 8],
                     "developer_status": "unknown", "developer_status_refs": []},
        "stage_signs": [{"stage": 3, "source_n": 2}],
        "problem_registry": [{"id": "delay", "in_scope": True, "status": "active",
                              "source_numbers": [2, 3, 6, 8]}],
        "research_evidence": {"evidence": [
            {"problem_id": "delay", "source_n": 6, "date_iso": "2026-01-30",
             "date_kind": "publication", "in_scope": True},
            {"problem_id": "delay", "source_n": 8, "date_iso": "2026-02-05",
             "date_kind": "publication", "in_scope": True},
        ]},
    }
    return sources, facts, qualification


def test_nagatino_keeps_direct_company_position_without_duplicate_flat_slot(nagatino):
    sources, facts, qualification = nagatino
    originals = deepcopy(nagatino)
    selected = evidence.select_pdf_evidence(sources, facts, qualification)
    assert len(selected.sources) <= 6
    assert {1, 2, 3, 6, 8} <= selected.mapping.keys()
    assert 5 not in selected.mapping
    assert 5 in selected.coverage["omitted_redundant"]
    assert selected.coverage["roles"]["company_position"] == [8]
    assert selected.coverage["required"]["company_cause"] == [8]
    assert all(selected.coverage["required"].values())
    assert all(f["source_n"] in range(1, 7) for f in selected.facts)
    assert [s["n"] for s in selected.sources] == list(range(1, len(selected.sources) + 1))
    assert nagatino == originals
    assert len(facts) > len(selected.facts)  # The wide research registry stays intact.
    projected_registry = evidence.fact_registry(selected.facts)
    for item in selected.coverage["evidence_summary"]["company_position"]:
        assert item["fact_key"] in projected_registry
        assert projected_registry[item["fact_key"]]["fact"]["source_n"] == item["source_n"]


def test_selection_is_generic_and_does_not_recognize_nagatino_or_publisher_domains(nagatino):
    sources, facts, qualification = deepcopy(nagatino)
    for row in [*sources, *facts]:
        for key in ("text", "title", "verbatim"):
            if isinstance(row.get(key), str):
                row[key] = row[key].replace("Nagatino I-Land", "Совсем другой жилой комплекс")
    for row in sources:
        row["url"] = row["url"].replace("developer.example", "builder.invalid")
    qualification["passport"]["official_name"] = "Совсем другой жилой комплекс"
    selected = evidence.select_pdf_evidence(sources, facts, qualification)
    assert {1, 2, 3, 6, 8} <= selected.mapping.keys()
    assert 5 not in selected.mapping


def test_first_party_interview_survives_when_incident_retelling_already_covers_stage_and_cause(
    nagatino,
):
    sources, facts, qualification = deepcopy(nagatino)
    # Match the production loss: the primary incident and stage proof also carry
    # an indirect company attribution, so ordinary company_position is covered.
    # The direct interview is not named in either stage or primary-problem refs.
    sources = [s for s in sources if s["n"] != 9]
    facts = [f for f in facts if f["source_n"] != 9]
    interview = next(s for s in sources if s["n"] == 8)
    interview["n"] = 9
    for row in facts:
        if row["source_n"] == 8:
            row["source_n"] = 9
    indirect = "По словам застройщика, причина переноса — проблемы с грунтом."
    incident = next(s for s in sources if s["n"] == 6)
    incident["title"] = "Nagatino I-Land: перенос ключей"
    incident["text"] += " " + indirect
    facts.append(fact(6, indirect, who="Девелопер", kind="claim", date="2026-01-30"))
    qualification["passport"].update(addressee_refs=[6], addressee_fact_indexes=[6])
    qualification["stage_signs"] = [{"stage": 3, "source_n": 6}]
    qualification["problem_registry"][0]["source_numbers"] = [2, 3, 6]
    qualification["research_evidence"]["evidence"] = [
        row for row in qualification["research_evidence"]["evidence"] if row["source_n"] != 8
    ]
    summary = evidence.evidence_summary(sources, facts, qualification)
    assert {row["source_n"] for row in summary["company_position"]} == {6, 9}
    assert {row["source_n"] for row in summary["first_party_company_position"]} == {9}
    selected = evidence.select_pdf_evidence(sources, facts, qualification, limit=6)
    assert 6 in selected.mapping and 9 in selected.mapping
    assert selected.coverage["required"]["first_party_company_position"] == [9]
    assert selected.coverage["required"]["current_stage"] == [6]
    assert selected.coverage["required"]["primary_incident"] == [6]
    assert len(selected.sources) <= 6 and 5 not in selected.mapping


def test_unique_dated_child_article_is_not_a_duplicate_card(nagatino):
    sources, facts, qualification = deepcopy(nagatino)
    new_position = "Застройщик сообщил: геологические исследования завершены."
    sources[4]["text"] += " " + new_position
    sources[4]["evidence_date"] = "2026-11-01"
    facts[5] = fact(5, new_position, who="Представитель застройщика", date="2026-11-01")
    assert not evidence._redundant(sources[0], sources[4], {
        1: [facts[0]], 5: [facts[5]]
    })


def test_company_role_alone_or_retold_action_is_not_direct_position():
    q = {"passport": {"developer_brand": "Девелопер"}}
    rows = [
        fact(1, "Готовность достигла 37%.", who="Президент Девелопера"),
        fact(1, "Застройщик Девелопер перенёс срок ключей.", who="Девелопер (по сообщению СМИ)"),
        fact(1, "Мы провели исследования из-за сложной геологии.", who="Президент Девелопера"),
    ]
    src = source(1, " ".join(f["verbatim"] for f in rows))
    summary = evidence.evidence_summary([src], rows, q)
    assert len(summary["company_position"]) == 1
    key = summary["company_position"][0]["fact_key"]
    assert evidence.fact_registry(rows)[key]["fact"]["verbatim"] == rows[2]["verbatim"]


def test_portfolio_interview_does_not_turn_other_project_cause_into_object_position():
    q = {"passport": {"official_name": "Остров", "developer_brand": "Девелопер"}}
    rows = [
        fact(1, "В проекте Остров мы провели исследования из-за сложной геологии.",
             who="Президент Девелопера"),
        fact(1, "В проекте Соседний мы изменили решения из-за новых стандартов.",
             who="Президент Девелопера"),
    ]
    src = source(1, " ".join(f["verbatim"] for f in rows))
    summary = evidence.evidence_summary([src], rows, q)
    assert len(summary["company_position"]) == len(summary["causes"]) == 1


def test_another_company_president_is_not_our_developers_public_position():
    quote = "В проекте Остров мы провели исследования из-за сложной геологии."
    q = {"passport": {"official_name": "Остров", "developer_brand": "Девелопер"}}
    row = fact(1, quote, who="Президент чужой компании")
    summary = evidence.evidence_summary([source(1, quote)], [row], q)
    assert summary["company_position"] == []


def test_required_proof_groups_fail_closed_instead_of_silently_dropping():
    sources = [source(n, f"Точный факт из документа {n}.") for n in range(1, 5)]
    facts = [fact(n, s["text"]) for n, s in enumerate(sources, 1)]
    q = {"passport": {"status": "stalled", "status_refs": [1],
                      "addressee": "developer_active", "addressee_refs": [2],
                      "developer_status": "bankruptcy", "developer_status_refs": [3]},
         "current_stage": 3, "stage_signs": [{"stage": 3, "source_n": 4}]}
    with pytest.raises(evidence.EvidenceSelectionError, match="не помещаются"):
        evidence.select_pdf_evidence(sources, facts, q, limit=3)


def test_set_cover_reuses_one_source_for_multiple_required_proofs():
    sources = [source(n, f"Точный факт из документа {n}.") for n in range(1, 4)]
    facts = [fact(n, s["text"]) for n, s in enumerate(sources, 1)]
    q = {"passport": {"status": "stalled", "status_refs": [1, 3],
                      "addressee": "developer_active", "addressee_refs": [2, 3]}}
    result = evidence.select_pdf_evidence(sources, facts, q, limit=1)
    assert set(result.mapping) == {3}
    assert all(values == [3] for values in result.coverage["required"].values())


def test_snippet_or_unreadable_page_cannot_cover_required_proof():
    sources = [dict(source(1, "Есть монолитные работы."), kind="snippet")]
    q = {"passport": {"status": "under_construction", "status_refs": [1]}}
    with pytest.raises(evidence.EvidenceSelectionError, match="Нет прочитанных"):
        evidence.select_pdf_evidence(sources, [fact(1, sources[0]["text"])], q)


def test_quote_options_require_exact_personal_buyer_speech_and_keep_original_text(nagatino):
    sources, facts, _ = nagatino
    options = evidence.buyer_quote_options(sources, facts, allowed_source_numbers={2})
    assert len(options) == 1
    option = options[0]
    assert option["text"] == facts[2]["verbatim"]
    assert option["refs"] == [2]
    assert option["attribution"] == "Источник 2, 16.03.2026"
    assert option["date"] == "16.03.2026"
    changed = deepcopy(facts)
    # Losing the final period still leaves an actual continuous source fragment.
    changed[2]["verbatim"] = "В моём договоре стоит декабрь 2026, позже срок перенесли"
    # A legitimate different continuous fragment has a different immutable key.
    assert evidence.buyer_quote_options(sources, changed, allowed_source_numbers={2})[0][
        "quote_key"
    ] != option["quote_key"]
    changed[2]["verbatim"] = "В моём договоре срок был совершенно другим."
    assert evidence.buyer_quote_options(sources, changed, allowed_source_numbers={2}) == []


def test_quote_keys_do_not_change_merely_because_pdf_sources_are_renumbered(nagatino):
    sources, facts, _ = deepcopy(nagatino)
    before = evidence.buyer_quote_options(sources, facts, allowed_source_numbers={2})[0]
    sources[1]["n"] = 6
    sources = [sources[1]]
    facts = [{**facts[2], "source_n": 6}]
    after = evidence.buyer_quote_options(sources, facts)[0]
    assert before["quote_key"] == after["quote_key"]
    assert after["refs"] == [6]


def test_quote_without_individual_date_never_inherits_last_post_or_page_date():
    quote = "Я жду ключи и прошу ответ по срокам."
    src = source(1, quote, "communities", date="2026-11-17")
    options = evidence.buyer_quote_options([src], [
        fact(1, quote, kind="claim", who="Собственник", date=None)
    ])
    assert options[0]["date"] is None
    assert options[0]["attribution"] == "Источник 1"


def draft(quote_keys, *, topics=None):
    return {
        "lead": "Есть подтверждённая проблема сроков.",
        "facts": [{"value": "2027", "label": "Ориентир", "refs": [1], "tone": "neutral"}]
        * 4,
        "timeline": [],
        "buyers": {"status": "Есть обращения", "note": "Покупатель сообщает о сроке.",
                   "note_refs": [1], "quotes": [{"quote_key": key} for key in quote_keys],
                   "topics": topics or []},
        "company": {"status": "Позиция опубликована", "points": []}, "gap": "Обязательства",
    }


def test_author_selects_only_quote_key_code_inserts_original_fragment(nagatino):
    options = evidence.buyer_quote_options(nagatino[0], nagatino[1], allowed_source_numbers={2})
    option = options[0]
    schema = evidence.situation_contract(options).model_json_schema()
    props = schema["$defs"]["BuyerQuoteChoice"]["properties"]
    assert set(props) == {"quote_key"}
    assert props["quote_key"].get("const") == option["quote_key"]
    original = draft([option["quote_key"]])
    result = evidence.resolve_situation(original, options)
    assert result.buyers.quotes[0].model_dump() == {
        k: option[k] for k in ("text", "attribution", "refs")
    }
    assert "text" not in original["buyers"]["quotes"][0]


def test_unknown_modified_and_repeated_quote_choices_are_not_fuzzily_repaired(nagatino):
    options = evidence.buyer_quote_options(nagatino[0], nagatino[1], allowed_source_numbers={2})
    option = options[0]
    with pytest.raises(ValidationError):
        evidence.resolve_situation(draft([option["quote_key"] + "x"]), options)
    value = draft([option["quote_key"]])
    value["buyers"]["quotes"][0]["text"] = "Улучшенная цитата от автора"
    with pytest.raises(ValidationError):
        evidence.resolve_situation(value, options)
    with pytest.raises(ValueError, match="повторный"):
        evidence.resolve_situation(draft([option["quote_key"]] * 2), options)


def test_no_real_quote_allows_supported_topics_but_not_fabrication():
    topics = [{"text": "Покупатели обсуждают срок передачи", "refs": [1]}]
    result = evidence.resolve_situation(draft([], topics=topics), [])
    assert result.buyers.quotes == [] and result.buyers.topics[0].text == topics[0]["text"]
    with pytest.raises(ValidationError):
        evidence.resolve_situation(draft(["q-made-up"], topics=topics), [])
    with pytest.raises(ValidationError, match="цитаты или темы"):
        evidence.resolve_situation(draft([]), [])


def test_reposted_third_person_and_company_words_are_not_buyer_quotes():
    raw = [
        fact(1, "По их словам, мы ждём ключи давно.", kind="claim", who="Дольщики"),
        fact(1, "Мы изменили сроки передачи.", kind="quote", who="Пресс-служба компании"),
        fact(1, "Мы предлагаем квартиры по выгодной цене.", kind="quote", who="Агент"),
    ]
    src = source(1, " ".join(f["verbatim"] for f in raw), "reviews")
    assert evidence.buyer_quote_options([src], raw) == []
