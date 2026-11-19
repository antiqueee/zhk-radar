"""Build a compact V8 observation from already verified, saved public evidence."""

from datetime import date as CalendarDate
from ipaddress import ip_address
from typing import Literal
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from radar.web.chats import redact
from radar.web.report_evidence import ResearchEvidence, date_span
from radar.web.sources import canonical_url

_MAX_SIGNALS = 4
_MAX_OFFICIAL = 3
_DATE_LABELS = {
    "publication": "Публикация",
    "review": "Отзыв",
    "message": "Сообщение",
    "event": "Дата в материале",
}
_CONTOURS = {
    "authorities": "Ведомства",
    "communities": "Сообщества",
    "media": "СМИ",
    "official": "Официальные источники",
    "reviews": "Отзывы",
}
_PRIVATE_PARAMS = {"token", "access_token", "key", "api_key", "apikey", "signature", "sig", "auth"}
_CHAT_HOSTS = {"t.me", "telegram.me", "telegram.org", "max.ru"}


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Meta(Model):
    schema_version: Literal["8"] = "8"
    mode: Literal["observation"] = "observation"
    date: CalendarDate
    layout: Literal["compact"] = "compact"


class Object(Model):
    name: str = Field(min_length=1, max_length=140)
    developer: str = Field(min_length=1, max_length=100)
    city: str = Field(min_length=1, max_length=90)


class Qualification(Model):
    label: Literal["Под наблюдением"] = "Под наблюдением"
    detail: str = Field(min_length=1, max_length=200)
    dated_evidence: int = Field(ge=0, le=100)
    active_contours: list[str] = Field(max_length=5)
    manual_checks: list[str] = Field(max_length=3)


class Source(Model):
    n: int = Field(ge=1)
    publisher: str = Field(min_length=1, max_length=65)
    title: str = Field(min_length=1, max_length=100)
    date: str = Field(min_length=1, max_length=30)
    url: str = Field(min_length=1, max_length=1000)


class Signal(Model):
    text: str = Field(min_length=1, max_length=210)
    source_n: int = Field(ge=1)
    date: str = Field(min_length=1, max_length=45)


class Official(Model):
    text: str = Field(min_length=1, max_length=210)
    title: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1, max_length=1000)


class Observation(Model):
    meta: Meta
    object: Object
    qualification: Qualification
    sources: list[Source] = Field(min_length=1, max_length=_MAX_SIGNALS)
    signals: list[Signal] = Field(min_length=1, max_length=_MAX_SIGNALS)
    official: list[Official] = Field(max_length=_MAX_OFFICIAL)

    @model_validator(mode="after")
    def validate_citations(self):
        numbers = {source.n for source in self.sources}
        if len(numbers) != len(self.sources) or {signal.source_n for signal in self.signals} != (
            numbers
        ):
            raise ValueError("Сигналы справки должны ссылаться на проверенные источники")
        if any(not _public_url(row.url) for row in [*self.sources, *self.official]):
            raise ValueError("Справка может ссылаться только на публичные источники")
        return self


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _excerpt(value, limit):
    if not isinstance(value, str):
        return ""
    text = " ".join(redact(value, []).split())
    if len(text) <= limit:
        return text
    front = text[: limit - 1]
    whole, _, _ = front.rpartition(" ")
    return (whole or front).rstrip(".,:;") + "…"


def _public_url(value):
    if not isinstance(value, str) or len(value) > 1000:
        return None
    try:
        url = canonical_url(value)
        parts = urlsplit(url)
    except (UnicodeError, ValueError):
        return None
    host = (parts.hostname or "").removeprefix("www.")
    if host in _CHAT_HOSTS or host.endswith((".local", ".internal", ".localhost", ".onion")):
        return None
    if any(key.casefold() in _PRIVATE_PARAMS for key, _ in parse_qsl(parts.query)):
        return None
    try:
        address = ip_address(host)
    except ValueError:
        suffix = host.rpartition(".")[2]
        return url if "." in host and (suffix.isalpha() or suffix.startswith("xn--")) else None
    return url if address.is_global and not address.is_multicast else None


def _date(value):
    span = date_span(value) if isinstance(value, str) else None
    if not span:
        return ""
    return span[0].strftime("%d.%m.%Y") if span[0] == span[1] else value


def _watch(payload):
    payload = _mapping(payload)
    pipeline = _mapping(payload.get("report_pipeline"))
    qualification = _mapping(payload.get("report_qualification"))
    research = _mapping(payload.get("report_research"))
    if not (
        str(pipeline.get("version")).removeprefix("report-kit-v") == "8"
        and pipeline.get("status") == qualification.get("verdict") == "watch"
        and pipeline.get("passed") is False
        and not pipeline.get("issue_kind")
        and not pipeline.get("issues")
        and not pipeline.get("overflow")
        and research.get("completed") is True
        and not research.get("errors")
    ):
        raise ValueError("Для справки нужен завершённый разбор V8 со статусом наблюдения")
    verified = qualification.get("research_evidence") or pipeline.get("research_evidence")
    return pipeline, qualification, ResearchEvidence.model_validate(verified)


def _snapshot_quotes(payload):
    """Keep existing checked card quotations eligible only inside the same saved source."""
    quotes = {}
    for raw in payload.get("evidence") or ():
        row = _mapping(raw)
        url = _public_url(row.get("url"))
        text = row.get("quote")
        if url and isinstance(text, str) and text.strip():
            quotes.setdefault(url.rstrip("/"), []).append(text)
    return quotes


def _display_sources(pipeline, research, snapshot_quotes):
    sources = {}
    raw_sources = pipeline.get("sources")
    facts = _mapping(pipeline.get("facts")).get("facts")
    if not isinstance(raw_sources, list) or not isinstance(facts, list):
        raise ValueError("В сохранённом разборе нет проверенных исходных материалов")
    for raw in raw_sources:
        row = _mapping(raw)
        number = row.get("n")
        if type(number) is not int or number < 1 or row.get("kind") != "page":
            continue
        if (
            row.get("source_kind") in {"chat", "snippet", "protected"}
            or row.get("read_status") not in {None, "loaded"}
            or row.get("messages")
        ):
            continue
        if url := _public_url(row.get("url")):
            sources[number] = (row, url)

    saved_sources, signals, used_urls = [], [], set()
    dated_saved = False
    ordered = sorted(
        (item for item in research.evidence if item.in_scope and item.source_n in sources),
        key=lambda item: (item.date_kind in _DATE_LABELS, item.date_iso or ""),
        reverse=True,
    )
    for item in ordered:
        day = _date(item.date_iso)
        day_label = _DATE_LABELS.get(item.date_kind)
        dated = bool(day and day_label)
        if dated_saved and not dated:
            break
        source, url = sources[item.source_n]
        source_text = source.get("text")
        if url in used_urls or not isinstance(source_text, str):
            continue
        quotes = []
        for index in item.fact_indexes:
            fact = _mapping(facts[index]) if 0 <= index < len(facts) else {}
            quote = fact.get("verbatim")
            if (
                fact.get("source_n") == item.source_n
                and isinstance(quote, str)
                and quote in source_text
            ):
                if text := _excerpt(quote, 210):
                    quotes.append(text)
        if not quotes:
            continue
        # The snapshot may retain a more precise quoted sentence from this same
        # publication than the anchor excerpt. It must still exist verbatim in it.
        direct = min(
            (text for text in snapshot_quotes.get(url.rstrip("/"), ()) if text in source_text),
            key=len,
            default="",
        )
        used_urls.add(url)
        dated_saved = dated_saved or dated
        anchor_quote = next((text for text in quotes if len(text) >= 55), quotes[0])
        signals.append({
            "text": _excerpt(direct, 210) or anchor_quote,
            "source_n": item.source_n,
            "date": day_label + " · " + day if dated else "Дата не указана",
        })
        publisher = _excerpt(source.get("publisher"), 65) or urlsplit(url).hostname
        saved_sources.append({
            "n": item.source_n,
            "publisher": _excerpt(publisher, 65) or "Источник",
            "title": _excerpt(source.get("title"), 100) or "Публикация",
            "date": _date(source.get("published_at")) or "Без даты публикации",
            "url": url,
        })
        if len(signals) == _MAX_SIGNALS:
            break
    return saved_sources, signals


def _official_evidence(evidence):
    saved = {}
    for raw in evidence:
        row = _mapping(raw)
        url = _public_url(row.get("url"))
        text = _excerpt(row.get("quote") or row.get("text"), 210)
        title = _excerpt(row.get("title"), 100)
        if url and text and title:
            saved.setdefault((url, text), {"text": text, "title": title, "url": url})
        if len(saved) == _MAX_OFFICIAL:
            break
    return list(saved.values())


def observation_data(payload, *, evidence=()):
    """Return validated layout data. Optional citations must be pre-verified by the caller."""
    pipeline, qualification, research = _watch(payload)
    sources, signals = _display_sources(pipeline, research, _snapshot_quotes(payload))
    coverage = _mapping(qualification.get("coverage"))
    active = coverage.get("active_contours") or ()
    if not isinstance(active, list):
        active = []
    detail = "Материалов недостаточно для итогового разбора."
    if qualification.get("current_stage") is None:
        detail += " Стадия конфликта не подтверждена."
    checks = ["Проверить свежие датированные обращения дольщиков и ответы застройщика."]
    if not coverage.get("official_deadline_found"):
        checks.append("Сверить сроки ввода и выдачи ключей по каждому корпусу.")
    if not coverage.get("developer_position_found"):
        checks.append("Найти датированное сообщение застройщика по вопросам дольщиков.")
    return Observation.model_validate({
        "meta": {"date": qualification.get("checked")},
        "object": {
            "name": _excerpt(research.passport.official_name or payload.get("name"), 140),
            "developer": _excerpt(research.passport.developer_brand, 100) or "Не установлен",
            "city": _excerpt(payload.get("region"), 90) or "Не уточнён",
        },
        "qualification": {
            "detail": detail,
            "dated_evidence": coverage.get("dated_evidence_12m", 0),
            "active_contours": [label for key, label in _CONTOURS.items() if key in active],
            "manual_checks": checks,
        },
        "sources": sources,
        "signals": signals,
        "official": _official_evidence(evidence),
    }).model_dump(mode="json")


def observation_available(payload):
    try:
        observation_data(payload)
    except ValueError:
        return False
    return True
