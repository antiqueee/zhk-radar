"""Counterexamples behind the editorial note, with no project-specific heuristics."""

import copy

import pytest

from radar.reports import prompts_v8
from radar.reports.editorial_brief import build_editorial_brief
from radar.reports.factual_copy import validate_copy_numbers
from radar.reports.research_anchors import fact_registry


@pytest.fixture
def materials():
    rows = [
        (
            1,
            "2026-01-30",
            "media",
            "claim",
            "дольщики",
            "Дольщики сообщают: на стройке котлован; в публикации названы 658 участников.",
        ),
        (
            2,
            "2026-10-28",
            "media",
            "fact",
            "публикация",
            "Готовность корпусов 1–3 — 37%; ведётся монтаж фасадов.",
        ),
        (
            3,
            "2026-02-05",
            "media",
            "quote",
            "Компания",
            "Компания объяснила: мы проводим исследования грунтов объекта X.",
        ),
        (
            4,
            None,
            "official",
            "fact",
            "карточка",
            "Плановый ввод — II квартал 2027 года.",
        ),
        (
            1,
            "2026-01-30",
            "media",
            "claim",
            "публикация",
            "Срок выдачи ключей перенесён с марта 2026 на декабрь 2027 года.",
        ),
        (
            5,
            "2026-02-16",
            "reviews",
            "claim",
            "участник",
            "Участник утверждает: потрачено 15 млрд рублей проектного финансирования.",
        ),
        (
            6,
            "2026-02-17",
            "communities",
            "claim",
            "участник",
            "Участник сообщил о направлении коллективного обращения в надзор.",
        ),
    ]
    sources = {}
    facts = []
    for number, day, contour, kind, who, text in rows:
        source = sources.setdefault(
            number,
            {
                "n": number,
                "title": "Объект X",
                "published_at": day,
                "date": day or "проверено 17.11.2026",
                "contour": contour,
                "text": "",
            },
        )
        source["text"] += text + "\n"
        facts.append(
            {
                "text": text,
                "date": day,
                "source_n": number,
                "kind": kind,
                "who_says": who,
                "verbatim": text,
            }
        )
    qualification = {
        "current_stage": 3,
        "passport": {
            "official_name": "Объект X",
            "developer_brand": "Компания",
            "phases": [{"completion_date": "2027-Q2", "keys_date": "2027-12"}],
        },
    }
    return list(sources.values()), {"facts": facts}, qualification


def keys(facts):
    return list(fact_registry(facts))


def test_every_fact_keeps_its_immutable_key_raw_kind_and_source_without_mutation(materials):
    sources, facts, qualification = materials
    coverage = {"loaded": 6, "warnings": ["Один форум без даты публикации"]}
    before = copy.deepcopy((sources, facts, qualification, coverage))
    brief = build_editorial_brief(sources, facts, qualification, "2026-11-17", coverage)
    assert [note["fact_key"] for note in brief["fact_notes"]] == keys(facts)
    assert [note["raw_kind"] for note in brief["fact_notes"]] == [
        fact["kind"] for fact in facts["facts"]
    ]
    assert [note["source_n"] for note in brief["fact_notes"]] == [1, 2, 3, 4, 1, 5, 6]
    assert all("verbatim" not in note and "text" not in note for note in brief["fact_notes"])
    brief["coverage"]["warnings"].append("локальная заметка")
    assert (sources, facts, qualification, coverage) == before


def test_direct_company_explanation_is_not_replaced_by_a_report_of_buyers_allegations(materials):
    sources, facts, qualification = materials
    brief = build_editorial_brief(sources, facts, qualification, "2026-11-17")
    assert brief["company_position"] == [{"fact_key": keys(facts)[2], "source_n": 3}]
    metadata = [{k: v for k, v in source.items() if k != "text"} for source in sources]
    assert (
        build_editorial_brief(metadata, facts, qualification, "2026-11-17")["company_position"]
        == []
    )


def test_completion_and_keys_are_distinct_and_no_passport_dates_are_merged(materials):
    sources, facts, qualification = materials
    brief = build_editorial_brief(sources, facts, qualification, "2026-11-17")
    groups = brief["role_groups_hint"]
    assert groups["commissioning"] == [keys(facts)[3]]
    assert groups["keys"] == [keys(facts)[4]]
    mixed = {
        **facts["facts"][3],
        "text": "Ввод — II кв. 2027, выдача ключей — 31.12.2027.",
        "verbatim": "Ввод — II кв. 2027, выдача ключей — 31.12.2027.",
    }
    facts["facts"].append(mixed)
    groups = build_editorial_brief(sources, facts, qualification, "2026-11-17")["role_groups_hint"]
    assert groups["mixed_deadlines"] == [keys(facts)[-1]]
    assert keys(facts)[-1] not in groups["commissioning"] + groups["keys"]
    assert qualification["passport"]["phases"] == [
        {"completion_date": "2027-Q2", "keys_date": "2027-12"}
    ]


def test_later_material_does_not_erase_an_earlier_allegation_or_describe_all_corps(materials):
    sources, facts, qualification = materials
    brief = build_editorial_brief(sources, facts, qualification, "2026-11-17")
    chronology = brief["construction_chronology_hint"]
    assert chronology["latest_dated_material_keys"] == [keys(facts)[1]]
    assert chronology["earlier_allegation_keys"] == [keys(facts)[0]]
    assert len(brief["fact_notes"]) == len(facts["facts"])
    # No current state, stage, percentage or corpus scope is inferred by the helper.
    assert "current_status" not in brief and "current_stage" not in brief
    assert set(chronology) == {
        "latest_dated_material_keys",
        "earlier_allegation_keys",
        "undated_material_keys",
    }


def test_publication_date_is_not_relabelled_as_the_date_a_deadline_changed(materials):
    sources, facts, qualification = materials
    brief = build_editorial_brief(sources, facts, qualification, "2026-11-17")
    note = brief["fact_notes"][4]
    assert note["source_date"] == "2026-01-30"
    assert note["source_date_basis"] == "published_at"
    assert note["fact_date_temporal"] == "past"
    assert note["event_date_role"] == "unclassified"
    assert "Дата публикации не доказывает дату изменения срока" in brief["rules"][2]


def test_financial_and_people_claims_never_become_verified_totals_or_combined_actions(materials):
    sources, facts, qualification = materials
    brief = build_editorial_brief(sources, facts, qualification, "2026-11-17")
    assert brief["role_groups_hint"]["financial"] == [keys(facts)[5]]
    assert {keys(facts)[0], keys(facts)[6]} <= set(brief["role_groups_hint"]["people_or_actions"])
    assert brief["fact_notes"][0]["raw_kind"] == "claim"
    assert brief["fact_notes"][5]["raw_kind"] == "claim"
    assert "не соединяй число из одного факта" in brief["rules"][5]
    assert not any("count" in field or "total" in field for field in brief)


@pytest.mark.parametrize(
    ("value", "expected", "span"),
    [
        ("2027-Q2", "future", ["2027-04-01", "2027-06-30"]),
        ("2026-Q3", "past", ["2026-07-01", "2026-09-30"]),
        ("2026", "overlaps_as_of", ["2026-01-01", "2026-12-31"]),
        ("проверено 17.11.2026", "unknown", None),
        ("недавно", "unknown", None),
        (None, "unknown", None),
    ],
)
def test_date_precision_and_unknown_values_are_preserved(materials, value, expected, span):
    sources, facts, qualification = materials
    facts["facts"][1]["date"] = value
    sources[1]["published_at"] = value
    sources[1]["date"] = value
    brief = build_editorial_brief(sources, facts, qualification, "2026-11-17")
    note = brief["fact_notes"][1]
    assert note["fact_date"] == value and note["fact_date_span"] == span
    assert note["fact_date_temporal"] == note["source_date_temporal"] == expected
    if expected in {"future", "overlaps_as_of", "unknown"}:
        assert (
            keys(facts)[1]
            not in brief["construction_chronology_hint"]["latest_dated_material_keys"]
        )


def test_overlapping_months_do_not_invent_an_order_and_missing_source_stays_annotated(materials):
    sources, facts, qualification = materials
    facts["facts"][0]["date"] = "2026-10"
    sources[0]["published_at"] = "2026-10"
    brief = build_editorial_brief(sources, facts, qualification, "2026-11-17")
    assert set(brief["construction_chronology_hint"]["latest_dated_material_keys"]) == {
        keys(facts)[0],
        keys(facts)[1],
    }
    assert brief["construction_chronology_hint"]["earlier_allegation_keys"] == []
    brief = build_editorial_brief(sources[1:], facts, qualification, "2026-11-17")
    assert len(brief["fact_notes"]) == len(facts["facts"])
    assert brief["fact_notes"][0]["source_date_temporal"] == "unknown"


def test_empty_facts_are_not_fabricated_to_make_a_brief(materials):
    sources, _, qualification = materials
    with pytest.raises(ValueError, match="нужны подтверждённые факты"):
        build_editorial_brief(sources, {"facts": []}, qualification, "2026-11-17")


def test_normalized_evidence_date_is_kept_with_its_provenance_not_checked_at(materials):
    sources, facts, qualification = materials
    sources[1].update(
        published_at=None,
        date="проверено 17.11.2026",
        evidence_date="2026-10-28",
        checked_at="2026-11-17T12:00:00Z",
    )
    brief = build_editorial_brief(sources, facts, qualification, "2026-11-17")
    assert brief["fact_notes"][1]["source_date_basis"] == "evidence_date"
    assert brief["fact_notes"][1]["source_date"] == "2026-10-28"
    assert brief["fact_notes"][1]["source_date_temporal"] == "past"


def test_source_publication_date_does_not_offer_a_date_for_an_undated_event(materials):
    sources, facts, qualification = materials
    # The live failure: a historical publication discusses changed future keys,
    # but none of its AtomicFacts proves the date on which that change occurred.
    sources[3].update(published_at="2024-07-26", date="2024-07-26")
    facts["facts"][3].update(
        date=None, text="Публикация сообщает о переносе выдачи ключей на декабрь 2027 года."
    )
    brief = build_editorial_brief(sources, facts, qualification, "2026-11-17")
    assert brief["fact_notes"][3]["source_date"] == "2024-07-26"
    assert brief["fact_notes"][3]["fact_date"] is None
    assert not any(option["source_n"] == 4 for option in brief["timeline_date_options"])
    assert not any(
        option["date_iso"].startswith("2024") for option in brief["timeline_date_options"]
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("28.10.2026", "2026-10-28"),
        ("2026-03", "2026-03"),
        ("II кв. 2027", "2027-Q2"),
        ("2 кв. 2026 года", "2026-Q2"),
        ("2026", "2026"),
        ("недавно", None),
        ("проверено 17.11.2026", None),
        (None, None),
    ],
)
def test_timeline_options_preserve_fact_date_precision_and_pass_the_unchanged_gate(
    materials, raw, expected
):
    sources, facts, qualification = materials
    facts["facts"][1]["date"] = raw
    options = build_editorial_brief(sources, facts, qualification, "2026-11-17")[
        "timeline_date_options"
    ]
    own = [option for option in options if option["source_n"] == 2]
    if expected is None:
        assert own == []
        return
    assert own == [{"source_n": 2, "date_iso": expected, "fact_keys": [keys(facts)[1]]}]
    validate_copy_numbers(expected, facts, [2], path="situation.timeline.0.date_iso")


def test_timeline_options_share_a_date_without_merging_proofs_from_other_sources(materials):
    sources, facts, qualification = materials
    options = build_editorial_brief(sources, facts, qualification, "2026-11-17")[
        "timeline_date_options"
    ]
    january = [option for option in options if option["date_iso"] == "2026-01-30"]
    assert january == [
        {"source_n": 1, "date_iso": "2026-01-30", "fact_keys": [keys(facts)[0], keys(facts)[4]]}
    ]
    assert "editorial_brief.timeline_date_options" in prompts_v8.SITUATION
    assert "Facts.date=null" in prompts_v8.SITUATION
    assert "evidence_roles.chronology" in prompts_v8.SITUATION
