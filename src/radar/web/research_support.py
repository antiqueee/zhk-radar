"""Narrow deterministic support checks for research labels, never new object facts."""

import re

from radar.reports.fact_spans import source_span
from radar.reports.factual_copy import validate_copy_numbers


def _plain(value):
    return re.sub(r"[^\w]+", " ", str(value or "").casefold().replace("ё", "е")).strip()


def company_position(fact, source, *, parties=(), contour=None, passport=None):
    """An attributed company statement can be published by a dated news outlet."""
    if (contour or source.get("contour")) not in {"official", "media", "authorities"}:
        return False
    if source.get("kind", source.get("source_kind")) == "snippet":
        return False
    quotation = fact.get("verbatim") or ""
    if not quotation or source_span(quotation, source.get("text") or "") is None:
        return False
    text = _plain(fact.get("text"))
    if re.search(
        r"не (?:прокомментир|ответил|отвеча|сообщ|разъясн)|без комментар|нет комментар", text
    ):
        return False
    # The projection uses the same strict quotation predicate. Import lazily to
    # keep report_evidence → anchors → projection out of module initialization.
    from radar.reports.pdf_evidence import _company_position

    identity = passport or dict(
        zip(("developer_brand", "developer_legal", "tech_customer"), parties, strict=False)
    )
    return _company_position(
        fact, {**source, "contour": contour or source.get("contour")}, identity
    )


def _events(text):
    text = _plain(text)
    found = set()
    if re.search(
        r"не (?:ответ|отвеч|прокоммент)|нет (?:прямого |официального )?ответ|"
        r"ждут.{0,25}ответ|не получили.{0,25}ответ",
        text,
    ):
        found.add("response_gap")
    if re.search(
        r"(?:обсужд|подготов|готов|планир).{0,50}(?:коллектив|обращен|жалоб)|сбор.{0,20}подпис|собирают.{0,20}подпис|объедин|самоорганизац|инициативн.{0,10}груп",
        text,
    ):
        found.add("organizing")
    prospective = re.search(r"планир|намерен|собираются|будут|будет|подготов|готовят", text)
    if not prospective and not re.search(r"не (?:обратил|направил|подал|подали)", text):
        if re.search(
            r"обратил|направил|подали|подал|обращен.{0,25}направлен|жалоб.{0,25}подан", text
        ):
            found.add("appeals_sent")
        if re.search(
            r"прош.{0,25}(?:митинг|пикет|акци)|состоя.{0,25}(?:митинг|пикет|акци)|вышли.{0,25}(?:улиц|митинг|пикет)|разбили.{0,20}лагер",
            text,
        ):
            found.add("street_action")
    if re.search(
        r"публикаци|опублик|репортаж|попал.{0,35}сми|сми.{0,25}(?:сообщ|напис|освет)", text
    ):
        found.add("media_publicity")
    if re.search(
        r"начат.{0,25}проверк|проверк.{0,25}начал|взя.{0,25}контрол|возбужд.{0,20}дело|надзор.{0,30}проверяет",
        text,
    ):
        found.add("authority_action")
    return found


def stage_sign_issues(text, facts, *, stage=None):
    """Numbers and event types must come from the linked atoms, not their whole pages.

    This is a conservative event-family guard, not general semantic entailment.
    Editorial review still checks every nuance and attribution after qualification.
    """
    issues = []
    try:
        validate_copy_numbers(text, facts, path="stage_sign")
    except ValueError as exc:
        issues.append(str(exc))
    proof = "\n".join(str(f.get("verbatim") or "") for f in facts)
    wanted, supported = _events(text), _events(proof)
    if not wanted:
        issues.append("Нет наблюдаемого события, подтверждающего стадию конфликта")
    elif wanted - supported:
        issues.append("Событие признака стадии не подтверждено связанными атомарными фактами")
    recipients = [
        r"прокурат|прокурор",
        r"следственн.{0,20}комитет|\bск\b",
        r"стройнадзор",
        r"\bсуд[а-я]*\b",
    ]
    for pattern in recipients:
        if re.search(pattern, _plain(text)) and not re.search(pattern, _plain(proof)):
            issues.append(
                "Адресат обращения или действие ведомства не подтверждены связанным фактом"
            )
    if re.search(r"в открытых источниках.{0,25}(?:нет|отсутств)|для всех.{0,25}нет", _plain(text)):
        if not re.search(
            r"в открытых источниках.{0,25}(?:нет|отсутств)|для всех.{0,25}нет", _plain(proof)
        ):
            issues.append("Частный запрос нельзя обобщить до отсутствия всех публичных разъяснений")
    allowed = {
        1: {"response_gap"},
        2: {"organizing", "appeals_sent"},
        3: {"appeals_sent", "street_action", "media_publicity", "authority_action"},
        4: {"street_action", "media_publicity"},
    }
    if stage and not (wanted & supported & allowed[stage]):
        issues.append("Связанное событие не подтверждает выбранную стадию")
    return list(dict.fromkeys(issues))


_APPEAL_AUTHORITY = re.compile(
    r"прокурат|прокурор|следственн.{0,20}комитет|\bск\b|надзор|ведомств|"
    r"\bсуд[а-я]*\b|полици|инспекц|\bвласт[а-я]*\b|мэри|администрац|президент"
)


def escalation_source_issues(text, facts, source, *, stage):
    """Require dated reporting or official evidence for legal escalation."""
    if stage < 3:
        return []
    events = _events(text)
    proof = "\n".join(str(f.get("verbatim") or "") for f in facts)
    legal_appeal = "appeals_sent" in events and _APPEAL_AUTHORITY.search(_plain(text + " " + proof))
    if "authority_action" not in events and not legal_appeal:
        return []
    if (
        source.get("contour") in {"official", "media", "authorities"}
        and source.get("kind", source.get("source_kind")) != "snippet"
        and (source.get("evidence_date") or any(f.get("date") for f in facts))
    ):
        return []
    return [
        "Обращение в ведомство или действие ведомства как признак эскалации "
        "требует датированного подтверждения из СМИ или официального источника"
    ]
