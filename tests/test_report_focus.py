import copy

import pytest

from radar.web.report_focus import LABELS, build_focus_context, validate_focus


@pytest.fixture
def inputs():
    sources = [
        {"n": n, "contour": contour, "kind": kind}
        for n, contour, kind in [
            (1, "official", "page"),
            (2, "media", "page"),
            (3, "communities", "chat"),
        ]
    ]
    facts = {"facts": [{"source_n": n, "date": "2026-11-05", "text": "Факт"} for n in range(1, 4)]}
    qualification = {
        "checked": "2026-11-16",
        "problem_registry": [
            {
                "id": "delay",
                "name": "Перенос сроков",
                "topic": "deadlines",
                "status": "active",
                "in_scope": True,
            },
            {
                "id": "apartments",
                "name": "Статус апартаментов",
                "topic": "other",
                "status": "active",
                "in_scope": True,
            },
        ],
        "research_evidence": {
            "evidence": [
                {
                    "problem_id": p,
                    "source_n": n,
                    "in_scope": True,
                    "date_kind": "publication" if n < 3 else "message",
                    "date_iso": "2026-11-05",
                }
                for p, n in [("delay", 1), ("delay", 2), ("apartments", 3)]
            ]
        },
    }
    payload = {
        "record_id": "one",
        "monitoring": {"score": 8, "as_of": "2026-11-05", "record_id": "one"},
    }
    return qualification, sources, facts, payload


def report(context):
    return {
        "diagnosis": {
            "main_problem": {
                "name": context["primary_problem"]["label"],
                "score": 8,
                "scale_note": context["monitoring"]["scale_note"],
                "causes": [{"refs": [1]}, {"refs": [2]}, {"refs": [3]}],
            },
            "extra_risk": {"points": [{"refs": [3]}, {"refs": [3]}]},
            "stage": {"signs": [{}, {}]},
            "stakeholders": [{}, {}, {}],
        },
        "situation": {"lead": "Дольщики ждут ключи", "facts": [], "timeline": []},
        "offer": {
            "usp": "Коммуникация о сроках",
            "why_now": [],
            "modules": [{"pain_refs": [1, 2]}],
        },
        "scenario": {"levels": [{"risks": [{"text": "Перенос срока"}]}]},
    }


def test_non_chat_primary_and_chat_secondary_are_separate(inputs):
    context = build_focus_context(*inputs)
    assert context["can_author"]
    assert context["primary_problem"]["id"] == "delay"
    assert context["primary_problem"]["non_chat_refs"] == [1, 2]
    assert context["extra_risk_candidate"]["id"] == "apartments"
    value = report(context)
    assert validate_focus(value, context) is value
    assert all(len(label) <= 22 for label in LABELS.values())


def test_chat_or_closed_post_delivery_problems_do_not_create_primary(inputs):
    q, sources, facts, payload = inputs
    sources[0]["contour"] = sources[1]["contour"] = "communities"
    assert not build_focus_context(q, sources, facts, payload)["can_author"]
    sources[0]["contour"] = "official"
    q["problem_registry"][0]["status"] = "closed"
    assert not build_focus_context(q, sources, facts, payload)["can_author"]
    q["problem_registry"][0].update(
        status="active", in_scope=False, topic="post_delivery", name="УК и парковка"
    )
    assert not build_focus_context(q, sources, facts, payload)["can_author"]


@pytest.mark.parametrize("path", ["lead", "usp", "triggers"])
def test_secondary_chat_theme_cannot_replace_main_line(inputs, path):
    context = build_focus_context(*inputs)
    value = report(context)
    if path == "lead":
        value["situation"]["lead"] = "Главная проблема — статус апартаментов"
    elif path == "usp":
        value["offer"]["usp"] = "Решаем статус апартаментов"
    else:
        value["scenario"]["levels"][0]["risks"] = [{"text": "Спор об апартаментах"}]
    with pytest.raises(ValueError, match="подменила"):
        validate_focus(value, context)


def test_extra_requires_proof_and_cannot_be_separate_service(inputs):
    context = build_focus_context(*inputs)
    value = report(context)
    value["offer"]["modules"] = [{"pain_refs": [3]}]
    with pytest.raises(ValueError, match="отдельное решение"):
        validate_focus(value, context)
    value = report(context)
    value["diagnosis"]["extra_risk"]["points"][0]["refs"] = [1]
    with pytest.raises(ValueError, match="extra_risk.points"):
        validate_focus(value, context)


def test_monitoring_exact_score_and_attribution_are_checked(inputs):
    context = build_focus_context(*inputs)
    assert (
        context["monitoring"]["allowed_score_min"],
        context["monitoring"]["allowed_score_max"],
    ) == (7, 9)
    value = report(context)
    value["diagnosis"]["main_problem"]["score"] = 6
    with pytest.raises(ValueError, match="расходится"):
        validate_focus(value, context)
    value = report(context)
    value["diagnosis"]["main_problem"]["scale_note"] = "Наша оценка сегодня"
    with pytest.raises(ValueError, match="дата и атрибуция"):
        validate_focus(value, context)


@pytest.mark.parametrize(
    "category,band", [("низкая", (1, 3)), ("средняя", (4, 6)), ("высокая", (7, 9))]
)
def test_qualitative_monitoring_does_not_invent_a_measurement(inputs, category, band):
    q, sources, facts, payload = inputs
    payload["monitoring"] = {"category": category, "date": "2026-11-05"}
    m = build_focus_context(q, sources, facts, payload)["monitoring"]
    assert m["valid"] and m["score"] is None
    assert (m["allowed_score_min"], m["allowed_score_max"]) == band


def test_missing_old_or_foreign_monitoring_is_not_measurement(inputs):
    q, sources, facts, payload = inputs
    for wrong in (
        {"score": 8},
        {"score": 8, "date": "2024-01-01"},
        {"score": 8, "date": "2026-11-05", "record_id": "other"},
    ):
        changed = copy.deepcopy(payload)
        changed["monitoring"] = wrong
        assert not build_focus_context(q, sources, facts, changed)["monitoring"]["valid"]


def test_saved_snapshot_is_the_same_canonical_monitoring_with_the_same_rejections(inputs):
    q, sources, facts, payload = inputs
    for value in (
        payload["monitoring"],
        {"category": "высокая", "date": "2026-11-05", "record_id": "one"},
    ):
        canonical = {**payload, "monitoring": value}
        saved = {"record_id": "one", "monitoring_snapshot": value}
        expected = build_focus_context(q, sources, facts, canonical)["monitoring"]
        assert expected["valid"]
        assert build_focus_context(q, sources, facts, saved)["monitoring"] == expected
    for value in (
        {"score": 8},
        {"score": 8, "date": "not-a-date"},
        {"score": 8, "date": "2024-01-01"},
        {"score": 8, "date": "2027-01-01"},
        {"score": "8", "date": "2026-11-05"},
        {"score": 11, "date": "2026-11-05"},
        {"score": True, "date": "2026-11-05"},
        {"score": 8, "date": "2026-11-05", "record_id": "another-object"},
    ):
        saved = {"record_id": "one", "monitoring_snapshot": value}
        assert not build_focus_context(q, sources, facts, saved)["monitoring"]["valid"]
