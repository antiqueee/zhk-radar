"""V8: evidence-led primary problem, secondary chat risk and dated monitoring score.

Inputs use the PDF projection's source numbers. This module never changes source
references, derives facts from a gold example or treats a chat report as a legal ruling.
"""

import re

from radar.web.report_evidence import as_of_date, date_span, fresh, source_contour

LABELS = {
    "deadlines": "Перенос сроков",
    "construction": "Ход строительства",
    "acceptance": "Приёмка объекта",
    "quality": "Качество объекта",
    "money": "Деньги и неустойка",
    "communication": "Ответы застройщика",
    "collective_action": "Коллективные действия",
    "developer_status": "Статус застройщика",
    "other": "Обязательства проекта",
}
TOPIC_ORDER = tuple(LABELS)
NON_CHAT = {"official", "authorities", "media", "reviews"}


def _data(value):
    return value.model_dump() if hasattr(value, "model_dump") else value


def _facts(value):
    value = _data(value)
    return value.get("facts", []) if isinstance(value, dict) else value


def _monitoring(payload, sources, as_of):
    value = (
        payload.get("monitoring")
        or payload.get("manual_monitoring")
        or payload.get("monitoring_snapshot")
    )
    if not isinstance(value, dict):
        value = next(
            (s["monitoring"] for s in sources if isinstance(s.get("monitoring"), dict)), {}
        )
    if not value and payload.get("monitoring_score") is not None:
        value = {"score": payload["monitoring_score"], "date": payload.get("monitoring_date")}
    if not value:
        return {"valid": False, "reason": "no_monitoring"}
    observed = value.get("as_of") or value.get("date")
    span = date_span(observed)
    if not span or span[0] != span[1] or not fresh(observed, as_of):
        return {"valid": False, "reason": "monitoring_date_unverified"}
    record_id = value.get("record_id")
    if record_id and payload.get("record_id") and record_id != payload["record_id"]:
        return {"valid": False, "reason": "monitoring_other_object"}
    score = value.get("score")
    category = str(value.get("category") or value.get("level") or "").casefold()
    bands = {
        "низкая": (1, 3),
        "low": (1, 3),
        "средняя": (4, 6),
        "medium": (4, 6),
        "высокая": (7, 9),
        "high": (7, 9),
    }
    if isinstance(score, int) and not isinstance(score, bool) and 1 <= score <= 10:
        low, high = max(1, score - 1), min(10, score + 1)
    elif category in bands:
        low, high = bands[category]
        score = None
    else:
        return {"valid": False, "reason": "monitoring_score_unverified"}
    day = span[0].strftime("%d.%m.%Y")
    return {
        "valid": True,
        "as_of": span[0].isoformat(),
        "score": score,
        "allowed_score_min": low,
        "allowed_score_max": high,
        "scale_note": "оценка мониторинга чатов на " + day,
    }


def build_focus_context(qualification, pdf_sources, pdf_facts, payload):
    """Return internal focus rules; can_author=False preserves a truthful manual result."""
    qualification = _data(qualification)
    sources = [_data(s) for s in pdf_sources]
    facts = [_data(f) for f in _facts(pdf_facts)]
    by_n = {s["n"]: s for s in sources}
    as_of = qualification.get("checked") or payload.get("as_of") or as_of_date().isoformat()
    evidence = (qualification.get("research_evidence") or {}).get("evidence", [])
    primary, secondary = [], []
    for problem in qualification.get("problem_registry", []):
        if problem.get("status") != "active":
            continue
        rows = [
            e
            for e in evidence
            if e.get("problem_id") == problem["id"]
            and e.get("date_kind") not in {"checked", "unknown"}
            and fresh(e.get("date_iso"), as_of)
        ]
        refs = sorted({e["source_n"] for e in rows if e["source_n"] in by_n})
        if not evidence and problem.get("active_dated_evidence_count", 0):
            refs = sorted(
                n
                for n in problem.get("source_numbers", [])
                if n in by_n
                and any(f.get("source_n") == n and fresh(f.get("date"), as_of) for f in facts)
            )
        refs = [n for n in refs if any(f.get("source_n") == n for f in facts)]
        if not refs:
            continue
        topic = problem.get("topic", "other")
        outside = [
            n
            for n in refs
            if source_contour(by_n[n]) in NON_CHAT
            and by_n[n].get("kind") not in {"chat", "chat-summary", "snippet"}
        ]
        chats = [
            n
            for n in refs
            if source_contour(by_n[n]) == "communities"
            or by_n[n].get("kind") in {"chat", "chat-summary"}
        ]
        label = (
            problem["name"] if len(problem["name"]) <= 22 else LABELS.get(topic, "Проблема проекта")
        )
        item = {
            "id": problem["id"],
            "name": problem["name"],
            "label": label,
            "topic": topic,
            "refs": refs,
            "non_chat_refs": outside,
            "chat_refs": chats,
            "evidence_count": len(rows) or problem.get("active_dated_evidence_count", 0),
        }
        if problem.get("in_scope") and outside:
            primary.append(item)
        elif chats and (
            problem.get("in_scope")
            or re.search(
                r"апартамент|предмет договор|перевод.*жил|назначени.*помещени",
                problem["name"],
                re.I,
            )
        ):
            secondary.append(item)
    primary.sort(
        key=lambda p: (
            -len(p["non_chat_refs"]),
            TOPIC_ORDER.index(p["topic"]) if p["topic"] in TOPIC_ORDER else 99,
            -p["evidence_count"],
        )
    )
    secondary.sort(key=lambda p: (-p["evidence_count"], -len(p["chat_refs"])))
    chosen = primary[0] if primary else None
    extra = secondary[0] if secondary else None
    extra_terms = []
    if extra and re.search(r"апартамент|перевод.*жил|назначени.*помещени", extra["name"], re.I):
        extra_terms = ["апартамент", "перевод в жил", "перевести в жил", "статус помещений"]
    return {
        "can_author": chosen is not None,
        "reason": None if chosen else "no_dated_non_chat_primary_problem",
        "primary_problem": chosen,
        "primary_candidates": primary,
        "extra_risk_candidate": extra,
        "secondary_candidates": secondary,
        "secondary_only_terms": extra_terms,
        "monitoring": _monitoring(payload, sources, as_of),
        "rules": {
            "primary_fields": [
                "situation.lead",
                "situation.facts",
                "situation.timeline",
                "offer.usp",
                "offer.why_now",
                "scenario.levels.0.risks",
            ],
            "secondary_is_separate_service": False,
            "scope": "dolshiki",
        },
    }


def _text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_text(v) for k, v in value.items() if k not in {"refs", "url"})
    if isinstance(value, list):
        return " ".join(_text(v) for v in value)
    return ""


def validate_focus(report, context):
    """Fail closed on focus/score errors; references and raw facts remain unchanged."""
    data = _data(report)
    primary = context.get("primary_problem")
    if not context.get("can_author") or not primary:
        raise ValueError("focus: нет датированной главной проблемы с подтверждением вне чатов")
    diagnosis = data["diagnosis"]
    main = diagnosis["main_problem"]
    errors = []
    if main["name"].casefold() != primary["label"].casefold():
        errors.append(
            "diagnosis.main_problem.name: требуется главная проблема «" + primary["label"] + "»"
        )
    cause_refs = {n for cause in main.get("causes", []) for n in cause.get("refs", [])}
    if not cause_refs.intersection(primary["non_chat_refs"]):
        errors.append("diagnosis.main_problem.causes: нет подтверждения главной проблемы вне чатов")
    extra = diagnosis.get("extra_risk")
    candidate = context.get("extra_risk_candidate")
    if candidate and not extra:
        errors.append("diagnosis.extra_risk: чатовая тема должна быть сохранена отдельно")
    if extra and not candidate:
        errors.append("diagnosis.extra_risk: нет подтверждённой дополнительной темы")
    if extra and candidate:
        if any(
            not set(point.get("refs", [])).intersection(candidate["refs"])
            for point in extra.get("points", [])
        ):
            errors.append("diagnosis.extra_risk.points: ссылка не подтверждает дополнительную тему")
        if (
            len(diagnosis["stage"]["signs"]) != 2
            or len(main["causes"]) != 3
            or len(diagnosis["stakeholders"]) != 3
        ):
            errors.append("diagnosis.extra_risk: нужны2признака,3причины,3группы для макета")
    checks = {
        "situation.lead": data["situation"].get("lead"),
        "situation.facts": data["situation"].get("facts"),
        "situation.timeline": data["situation"].get("timeline"),
        "offer.usp": data["offer"].get("usp"),
        "offer.why_now": data["offer"].get("why_now"),
        "scenario.triggers": data["scenario"]["levels"][0].get("risks"),
    }
    for path, value in checks.items():
        if any(term in _text(value).casefold() for term in context.get("secondary_only_terms", [])):
            errors.append(path + ": дополнительная чатовая тема подменила основную линию")
    if candidate:
        for index, module in enumerate(data["offer"].get("modules", [])):
            if (
                set(module.get("pain_refs", []))
                and set(module["pain_refs"]) <= set(candidate["refs"])
                and not set(module["pain_refs"]).intersection(primary["refs"])
            ):
                errors.append(
                    f"offer.modules/{index}: отдельное решение под дополнительную чатовую тему"
                )
    monitoring = context.get("monitoring") or {}
    if monitoring.get("valid"):
        if not monitoring["allowed_score_min"] <= main["score"] <= monitoring["allowed_score_max"]:
            errors.append(
                "diagnosis.main_problem.score: оценка расходится с датированным мониторингом"
            )
        if main.get("scale_note") != monitoring["scale_note"]:
            errors.append(
                "diagnosis.main_problem.scale_note: требуется дата и атрибуция мониторинга"
            )
    if errors:
        raise ValueError("; ".join(errors))
    return report
