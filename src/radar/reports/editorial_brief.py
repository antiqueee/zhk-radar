"""Small deterministic author notes; annotations are not new evidence or qualification."""

import re
from copy import deepcopy

from radar.reports.fact_numbers import date_literals
from radar.reports.pdf_evidence import evidence_summary
from radar.reports.research_anchors import fact_registry
from radar.web.report_evidence import date_span

_ROLES = {
    "commissioning": r"ввод|эксплуатац|введ[её]н|ввести",
    "keys": r"ключ|передач|передад|переда[её]т",
    "construction_material": (
        r"стро[ий]|строител|готовност|монтаж|фасад|фундамент|котлован|ход работ"
    ),
    "allegation": (
        r"жалоб|претензи|утвержда|по словам|сообща[ею]т дольщик|котлован|останов|стройка стоит"
    ),
    "financial": r"финанс|эскроу|неустойк|средств|деньг|млн|млрд|рубл|₽",
    "people_or_actions": r"человек|дольщик|подпис|участник|обращен|петиц|жалоб|голос",
}
_PATTERNS = {name: re.compile(pattern, re.I) for name, pattern in _ROLES.items()}
RULES = [
    "Роли — лексические подсказки, не подтверждение проблемы, причины или стадии. "
    "Каждый fact_key ссылается на неизменный исходный Fact; его kind и атрибуция сохраняются.",
    "Ввод и выдача ключей — разные события. Бери даты и корпус из соответствующего "
    "факта; не объединяй даты паспортных фаз и не назначай дату из смешанного факта по роли.",
    "Дата публикации не доказывает дату изменения срока. Если дата изменения не названа, "
    "пиши «публикация от … сообщила», а не «в этот день срок перенесли». Проверено — не событие. "
    "Границы интервала даты нужны для сравнения, а не для назначения дня событию. "
    "В timeline допустимы только timeline_date_options из Facts.date, не source_date и не "
    "evidence_roles.chronology. При date=null источник не даёт дату событию даже при известной "
    "дате публикации; такой материал можно описать вне timeline без назначения дня событию.",
    "Поздний материал о работах не отменяет прошлую жалобу. Ранняя жалоба о котловане или "
    "остановке не описывает автоматически состояние сейчас. Сверь корпус и вид источника.",
    "Прямую позицию компании бери только из company_position. Публикация чужой жалобы "
    "или пересказ участника не становится заявлением компании. Известное объяснение не скрывай.",
    "Финансовое или количественное утверждение сохраняет исходный статус и источник. "
    "Не превращай заявления о суммах/людях в проверенный итог; не соединяй число из одного "
    "факта с подписями, обращениями или действиями из другого, не вычисляй новые показатели.",
    "Предлагай стратегический результат и работу агентства по доказанной проблеме: "
    "переговоры, коммуникацию изменений, сложные обращения. Не продавай застройщику "
    "выяснение известного ему статуса; не изобретай причины, численность или гарантии.",
]


def _data(value):
    return value.model_dump() if hasattr(value, "model_dump") else value


def _span(value):
    span = date_span(value)
    return [day.isoformat() for day in span] if span else None


def _temporal(value, as_of):
    span, today = date_span(value), date_span(as_of)
    if not span or not today:
        return "unknown"
    if span[1] < today[0]:
        return "past"
    if span[0] > today[1]:
        return "future"
    return "as_of" if span == today else "overlaps_as_of"


def _timeline_iso(value):
    dates = date_literals(str(value or ""))
    if len(dates) != 1 or not date_span(value):
        return None
    year, month, day = dates[0][2]
    if month == "q":
        candidate = f"{year}-Q{day}"
    else:
        candidate = str(year) + (f"-{month:02d}" if month else "")
        candidate += f"-{day:02d}" if day else ""
    return candidate if date_span(candidate) == date_span(value) else None


def build_editorial_brief(sources, facts, qualification, as_of, coverage=None):
    """Annotate every validated fact without copying verbatims or mutating inputs.

    Full readable sources let evidence_summary identify exact grounded company
    speech. Metadata-only sources deliberately do not create company positions.
    Publication chronology does not classify a mentioned deadline as an event.
    """
    sources = [_data(source) for source in sources]
    qualification = _data(qualification)
    by_n = {source["n"]: source for source in sources}
    registry = fact_registry(facts)
    notes, groups, dated = [], {name: [] for name in (*_ROLES, "mixed_deadlines")}, []
    timeline_dates = {}
    for key, row in registry.items():
        fact = row["fact"]
        iso = _timeline_iso(fact.get("date"))
        if iso:
            timeline_dates.setdefault((fact["source_n"], iso), []).append(key)
        source = by_n.get(fact["source_n"], {})
        text = " ".join(str(fact.get(field) or "") for field in ("text", "verbatim"))
        roles = [name for name, pattern in _PATTERNS.items() if pattern.search(text)]
        if "commissioning" in roles and "keys" in roles:
            roles.append("mixed_deadlines")
        for role in roles:
            if role not in {"commissioning", "keys"} or "mixed_deadlines" not in roles:
                groups[role].append(key)
        source_basis = next(
            (
                field
                for field in (
                    "published_at",
                    "evidence_date",
                    "published_date",
                    "publication_date",
                    "date",
                )
                if date_span(source.get(field))
            ),
            "unknown",
        )
        publication = (
            source.get(source_basis)
            if source_basis != "unknown"
            else (source.get("published_at") or source.get("date"))
        )
        publication_span = _span(publication)
        temporal = _temporal(publication, as_of)
        notes.append(
            {
                "fact_key": key,
                "source_n": fact["source_n"],
                "raw_kind": fact.get("kind"),
                "roles_hint": roles,
                "fact_date": fact.get("date"),
                "fact_date_span": _span(fact.get("date")),
                "fact_date_temporal": _temporal(fact.get("date"), as_of),
                "source_date_basis": source_basis,
                "source_date": publication,
                "source_date_span": publication_span,
                "source_date_temporal": temporal,
                "event_date_role": "unclassified",
            }
        )
        if "construction_material" in roles and publication_span and temporal in {"past", "as_of"}:
            dated.append((key, publication_span, roles))
    # Overlapping partial dates cannot establish which material is newer.
    latest = [key for key, span, _ in dated if not any(other[0] > span[1] for _, other, _ in dated)]
    earlier = [
        key
        for key, span, roles in dated
        if "allegation" in roles and any(other[0] > span[1] for k, other, _ in dated if k in latest)
    ]
    return {
        "version": "editorial-notes-v2",
        "as_of": as_of,
        "fact_notes": notes,
        "timeline_date_options": [
            {"source_n": n, "date_iso": iso, "fact_keys": keys}
            for (n, iso), keys in timeline_dates.items()
        ],
        "role_groups_hint": groups,
        "company_position": evidence_summary(sources, facts, qualification)["company_position"],
        "construction_chronology_hint": {
            "latest_dated_material_keys": latest,
            "earlier_allegation_keys": earlier,
            "undated_material_keys": [
                note["fact_key"]
                for note in notes
                if "construction_material" in note["roles_hint"]
                and note["source_date_temporal"] == "unknown"
            ],
        },
        "coverage": deepcopy(coverage),
        "rules": list(RULES),
    }
