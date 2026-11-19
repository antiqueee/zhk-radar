"""Wide research → qualification → authored V8 → independent critic → checked PDF.

The source registry is deliberately wider than the six citations printed in the PDF.
No draft is downloadable. Stage repairs and editorial revisions have bounded budgets.
"""

import hashlib
import json
import re
from copy import deepcopy
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, ValidationError

from radar.reports import (
    copy_edit,
    editorial_brief,
    editorial_evidence,
    factual_copy,
    matrix,
    pdf_evidence,
    research_anchors,
    review_v8,
    schema,
    source_copy,
)
from radar.reports import prompts_v8 as prompts
from radar.reports import schema_v8 as contract
from radar.reports.author_output import AUTHOR_TOOL, parse_author_json, parse_author_tool
from radar.reports.fact_numbers import date_literals
from radar.reports.fact_spans import normalized_with_spans, source_span
from radar.reports.model_retry import retry_model_call
from radar.reports.pipeline import buyer_quotes, services, walk
from radar.reports.render import render, template_hash, write_pdf_cache
from radar.reports.scenario_v8 import PLAYBOOK, build_scenario, stages_block
from radar.reports.serious_claims import (
    asserts_filing_heading,
    authority_claim_issues,
    is_attributed_filing,
    normalize_forum_report_voice,
    serious_terms,
    without_reported_filing_authority_names,
)
from radar.reports.v8 import PRODUCTS, RULES, check, valid_fact_value
from radar.web import report_evidence, report_focus
from radar.web.brief_service import payload_for
from radar.web.chats import redact
from radar.web.configuration import configuration, model_config, model_options
from radar.web.discovery import call_json
from radar.web.evidence_context import sources_for
from radar.web.model_gateway import ModelCallError, routed_client
from radar.web.model_output import parse_model_json, structured_format
from radar.web.record_locks import REPORT_LOCKS
from radar.web.report_evidence import (
    internal_priority,
    monitoring_scale,
    normalize_research,
    normalize_sources,
    qualify,
    research_context,
)
from radar.web.report_focus import build_focus_context, validate_focus
from radar.web.sources import canonical_url, is_access_page, primary_text
from radar.web.tasks import check_task_cancelled, report_progress
from radar.web.workflow import cached, discard_cached

VERSION = "report-kit-v8"
ROOT = Path(__file__).parent
_SOURCE_LIMIT = 40
_COPY_REPAIRS_PER_PAGE = {"situation": 8, "assessment": 3, "scenario": 2, "offer": 3}
_ESCALATION_BY_DATE = re.compile(
    r"\b(?:(?:сниж\w*|сниз\w*|сокращ\w*|уменьш\w*)\s+(?:риск\w*|вероятност\w*)|"
    r"предотвращ\w*|предотврат\w*|не\s+допуст\w*|сдерж\w*|удерж\w*)"
    r"[^.!?;]{0,60}?\b(?:эскалац\w*|стади\w+\s+(?:№\s*)?[1-4]\b|"
    r"на\s+(?:текущ\w+|той\s+же)\s+стади\w+)"
    r"\s+(?:вплоть\s+до|до|к)\s+(?:(?:конц[ау]|начал[ау]|середин[ые])\s+)?",
    re.I,
)
_GROWING_READINESS_GAP = re.compile(
    r"\b(?:разрыв\w*\b[^.!?]{0,35}\b(?:нарастает|раст[её]т)|"
    r"темп\w*\s+стройк\w*\s+вызыва\w*\s+сомнени\w*)\b", re.I,
)
_UNPROVEN_LOCALIZATION = re.compile(
    r"^Конфликт локализован на форуме и в (?:городских\s+)?СМИ\s*[–—]\s+", re.I,
)


class ResearchFact(contract.AtomicFact):
    source_n: int = Field(ge=1)


class ResearchFacts(schema.Model):
    facts: list[ResearchFact] = Field(min_length=1, max_length=300)


class FactsBatch(schema.Model):
    facts: list[ResearchFact] = Field(min_length=0, max_length=24)


class RawFactsEnvelope(schema.Model):
    """Validate the document shape before validating each independent atom."""

    facts: list[object] = Field(min_length=0, max_length=24)


def parse_fact_batch(response, *, issues, batch):
    envelope = parse_model_json(response, RawFactsEnvelope, repair_arrays=True)
    valid, indices = [], []
    for index, raw in enumerate(envelope.facts):
        try:
            fact = ResearchFact.model_validate(raw)
        except ValidationError as exc:
            for error in exc.errors():
                suffix = "/".join(map(str, error["loc"]))
                issues.append(
                    {
                        "batch": batch,
                        "path": f"facts/{index}" + ("/" + suffix if suffix else ""),
                        "source_n": raw.get("source_n") if isinstance(raw, dict) else None,
                        "reason": error["msg"],
                        "validation_type": error["type"],
                        "fact": raw,
                    }
                )
            continue
        valid.append(fact)
        indices.append(index)
    return FactsBatch(facts=valid), indices


class NeedsReview(ValueError):
    """An editorial failure, distinct from transport, key or balance failures."""


class InvalidModelOutput(NeedsReview):
    """Model output remained unreadable after the allowed repair attempts."""


class CopyValidationError(ValueError):
    def __init__(self, issues):
        self.issues = issues
        super().__init__("; ".join(row["path"] + ": " + row["reason"] for row in issues))


def _source_key(source):
    if source.get("url"):
        return canonical_url(source["url"])
    return str(
        source.get("id")
        or source.get("source_id")
        or source.get("chat_id")
        or ("anonymous:" + hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest())
    )


def _semantic_source(source):
    fields = {
        "n",
        "url",
        "text",
        "title",
        "publisher",
        "contour",
        "type",
        "kind",
        "source_kind",
        "published_at",
        "evidence_date",
        "event_date",
        "publication_date_provenance",
        "count",
        "messages",
        "read_status",
    }
    return {**{k: v for k, v in source.items() if k in fields}, "identity": _source_key(source)}


def registry_sources(payload):
    """Group a whole chat/page without requiring a fabricated URL for uploaded chats."""
    grouped = {}
    candidates = sorted(
        sources_for(payload), key=lambda row: row.get("origin") == "brand_analytics"
    )
    for source in candidates:
        text = redact(source.get("text") or "", []).strip()
        if not text:
            continue
        url = source.get("url") or None
        if url:
            try:
                url = canonical_url(url)
            except ValueError:
                continue
        text = primary_text(url or "", text).strip()
        if not text:
            continue
        key = url or _source_key(source)
        if key not in grouped:
            if source.get("origin") == "brand_analytics" and len(grouped) >= _SOURCE_LIMIT:
                continue
            grouped[key] = {**source, "url": url, "text": text}
        elif source.get("origin") != "brand_analytics" and text not in grouped[key]["text"]:
            grouped[key]["text"] += "\n" + text
    # Enrichment can insert documents before chat evidence. Persisted source
    # identities keep fact references stable across that merge and the next run.
    assigned = (payload.get("report_research") or {}).get("source_registry_numbers") or {}
    used, numbered = set(), []
    next_n = max((n for n in assigned.values() if isinstance(n, int) and n > 0), default=0)
    for key, source in grouped.items():
        n = assigned.get(key)
        if not isinstance(n, int) or n <= 0 or n in used:
            next_n += 1
            n = next_n
        used.add(n)
        numbered.append({**source, "n": n})
    result = sorted(normalize_sources(numbered), key=lambda row: row["n"])
    return result


def source_batches(sources, limit=24000):
    """Bound model input with exact chunks; never truncate saved source text."""
    batch, size = [], 0
    for source in sources:
        if source.get("kind") == "snippet" or source.get("source_kind") == "snippet":
            continue
        if is_access_page(source.get("title", ""), source.get("text", "")):
            continue
        text = source["text"]
        # A chunk boundary affects input only. Verbatim is checked against the full source.
        for start in range(0, len(text), limit):
            part = {k: v for k, v in source.items() if k != "messages"}
            part["text"] = text[start : start + limit]
            if batch and size + len(part["text"]) > limit:
                yield batch
                batch, size = [], 0
            batch.append(part)
            size += len(part["text"])
    if batch:
        yield batch


def ground_facts(facts, sources, *, issues=None):
    """Collect every bad field; restore only exact, uniquely mapped source spans."""
    from radar.reports.fact_numbers import canonical_fact_date, validate_fact_numbers

    result = facts.model_copy(deep=True)
    by_n = {s["n"]: s for s in sources}
    prepared, errors = {}, []
    for index, fact in enumerate(result.facts):
        prefix = f"facts/{index}"
        source = by_n.get(fact.source_n)
        if not source:
            errors.append({"path": prefix + "/source_n", "reason": "Нет такого источника"})
            continue
        if source.get("kind") == "snippet" or source.get("source_kind") == "snippet":
            errors.append(
                {
                    "path": prefix + "/source_n",
                    "source_n": fact.source_n,
                    "reason": "Это поисковый сниппет: факт из него извлекать нельзя",
                }
            )
            continue
        if is_access_page(source.get("title", ""), source.get("text", "")):
            errors.append(
                {
                    "path": prefix + "/source_n",
                    "source_n": fact.source_n,
                    "reason": "Страница проверки доступа не является материалом об объекте",
                }
            )
            continue
        if fact.source_n not in prepared:
            prepared[fact.source_n] = normalized_with_spans(source["text"])
        span = source_span(fact.verbatim, source["text"], prepared[fact.source_n])
        if span is None:
            quote = fact.verbatim
            exact_elsewhere = [s["n"] for s in sources if quote and quote in s["text"]]
            match = SequenceMatcher(
                None, quote, source["text"], autojunk=False
            ).find_longest_match()
            left = max(0, match.b - match.a - 40)
            errors.append(
                {
                    "path": prefix + "/verbatim",
                    "source_n": fact.source_n,
                    "reason": "Нет точного фрагмента: скопируй исходный текст без перефраза",
                    "provided": quote,
                    "exact_source_numbers": exact_elsewhere,
                    "source_excerpt": source["text"][left : left + min(len(quote) + 120, 700)],
                }
            )
        else:
            fact.verbatim = span
        numeric_issues = validate_fact_numbers(fact.model_dump(), source)
        for issue in numeric_issues:
            errors.append({"path": prefix, "source_n": fact.source_n, "reason": issue})
        if not numeric_issues:
            fact.date = canonical_fact_date(fact.date, source)
    if errors and issues is not None:
        failed = {int(error["path"].split("/")[1]) for error in errors}
        for error in errors:
            index = int(error["path"].split("/")[1])
            issues.append(
                {
                    **error,
                    "source_n": result.facts[index].source_n,
                    "fact": facts.facts[index].model_dump(),
                }
            )
        return facts.__class__(facts=[f for i, f in enumerate(result.facts) if i not in failed])
    if errors:
        raise ValueError(
            json.dumps(
                {
                    "fact_errors": errors,
                    "instruction": "Исправь только перечисленные поля. Сохрани остальные факты. "
                    "Недоказуемый факт удали; не сочиняй другой.",
                },
                ensure_ascii=False,
            )
        )
    return result


def filter_fact_batch(parsed, sources, *, issues, batch):
    value, original_indices = parsed
    grounding_issues = []
    kept = ground_facts(value, sources, issues=grounding_issues)
    for issue in grounding_issues:
        parts = issue["path"].split("/")
        parts[1] = str(original_indices[int(parts[1])])
        issues.append({**issue, "batch": batch, "path": "/".join(parts)})
    return kept


def project_short_name(name):
    value = re.sub(r'[«»„“”‟"‹›‘’]', "", name)
    value = re.sub(r"^\s*(?:жилой\s+комплекс|жк)\b\s*", "", value, flags=re.I)
    without_brand = re.sub(r"^\s*светлый\s+мир\b\s*[.,:—–-]?\s*", "", value, flags=re.I)
    return re.sub(r"\s+", " ", without_brand or value).strip(" .,;:—–-")


def _project_name_key(value):
    return re.sub(r"[^\w]+", " ", value.casefold().replace("ё", "е")).strip()


def usp_mentions_object(text, context):
    passport = context.get("qualification", {}).get("passport", {})
    aliases = [context["object"][key] for key in ("name", "short_name")]
    aliases.extend(passport.get("aliases") or [])
    target = _project_name_key(text)
    for alias in aliases:
        key = _project_name_key(project_short_name(alias))
        meaningful = re.sub(
            r"\b(?:жилой комплекс|жк|светлый мир|корпус|дом|очередь|строение|этап)\b", "", key
        )
        if len(re.sub(r"[\W\d_]+", "", meaningful)) < 4:
            continue
        pattern = r"(?<!\w)" + r"\s*".join(re.escape(part) for part in key.split()) + r"(?!\w)"
        if re.search(pattern, target):
            return True
    return False


def select_pdf_sources(sources, facts, qualification):
    """Reserve distinct proof roles, including the company's direct explanation."""
    result = pdf_evidence.select_pdf_evidence(
        sources,
        facts,
        qualification,
        limit=4 if qualification["verdict"] == "report_compact" else 6,
    )
    qualification["pdf_evidence_selection"] = result.coverage
    return result.sources, ResearchFacts(facts=result.facts), result.mapping


def metadata(sources):
    result = []
    for source in sources:
        count = source.get("count") or source.get("message_count")
        kind = source["type"]
        if kind not in RULES["source_types"]:
            kind = {
                "official": "карточка объекта",
                "authorities": "ведомство",
                "media": "публикация",
                "reviews": "отзывы",
                "communities": "чат",
                "other": "публикация",
            }[source["contour"]]
        publisher = source["publisher"]
        if len(publisher) > 20:
            # Keep the exact link; an overlong unmapped hostname is not a display name.
            publisher = "Материалы объекта"
        result.append(
            {
                "n": source["n"],
                "url": source.get("url"),
                "publisher": publisher,
                "date": source["display_date"],
                "type": kind,
                "count": count if isinstance(count, int) and count >= 5 else None,
            }
        )
    return result


def author_qualification(value, mapping):
    """PDF references have their own namespace; internal gaps never become PDF prose."""
    result = json.loads(
        json.dumps(
            {
                key: value[key]
                for key in (
                    "checked",
                    "verdict",
                    "client_role",
                    "passport",
                    "current_stage",
                    "stage_signs",
                    "problem_registry",
                )
                if key in value
            }
        )
    )
    result["problem_registry"] = [
        {
            key: p[key]
            for key in ("id", "name", "topic", "in_scope", "status", "corpuses", "source_numbers")
            if key in p
        }
        for p in result.get("problem_registry", [])
        if p["in_scope"]
        and p["status"] == "active"
        and set(p.get("source_numbers", [])) & mapping.keys()
    ]
    result["stage_signs"] = [s for s in result.get("stage_signs", []) if s["source_n"] in mapping]
    for node in walk(result):
        for key, value in list(node.items()):
            if key == "fact_indexes" or key.endswith("_fact_indexes"):
                node.pop(key)
            elif key.endswith("refs") or key == "source_numbers":
                node[key] = [mapping[n] for n in value if n in mapping]
            elif key == "source_n":
                node[key] = mapping.get(value)
    result["passport"]["phases"] = [
        p for p in result["passport"].get("phases", []) if p.get("refs")
    ]
    return result


def named_nodes(value, path):
    """Walk dictionaries with concrete leaf addresses for actionable validation."""
    if isinstance(value, dict):
        yield path, value
        for key, child in value.items():
            yield from named_nodes(child, path + "." + key)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from named_nodes(child, path + "." + str(index))


def validate_serious_copy(blocks, facts, sources):
    issues = []
    trusted = {
        s["n"] for s in sources
        if s["contour"] in {"official", "media", "authorities"}
        and (s.get("evidence_date") or any(f.date for f in facts.facts if f.source_n == s["n"]))
    }
    for block, value in blocks.items():
        for path, node in named_nodes(value, block):
            refs = {r for k, v in node.items() if k.endswith("refs") for r in v}
            if "n" in node and "publisher" in node:
                refs.add(node["n"])
            for key in ("text", "label", "title", "says"):
                text = str(node.get(key) or "")
                serious = serious_terms(
                    without_reported_filing_authority_names(text), RULES["needs_official_or_media"]
                )
                if serious and not refs & trusted:
                    issues.append({"path": path + "." + key, "reason": (
                        "Серьёзное утверждение требует датированного СМИ или ведомства. "
                        "Убери неподтверждённую юридическую конкретизацию, сохрани "
                        "подтверждённый предмет жалобы без добавления ссылок: " + text
                    )})
    lead = without_reported_filing_authority_names((blocks.get("situation") or {}).get("lead", ""))
    for stem in serious_terms(lead, RULES["needs_official_or_media"]):
        if not any(
            f.source_n in trusted and stem in serious_terms(
                f.text + " " + f.verbatim, RULES["needs_official_or_media"]
            )
            for f in facts.facts
        ):
            issues.append({"path": "situation.lead", "reason": (
                "Серьёзное утверждение требует датированного СМИ или ведомства"
            )})
    issues.extend(authority_claim_issues(blocks, facts, sources))
    if issues:
        raise CopyValidationError(issues)


def validate_authority_copy(blocks, facts, sources):
    issues = authority_claim_issues(blocks, facts, sources)
    if issues:
        raise CopyValidationError(issues)


def validate_editorial_copy(blocks, facts):
    issues = editorial_evidence.editorial_claim_issues(blocks, facts)
    if issues:
        raise CopyValidationError(issues)


def promises_to_limit_escalation_by_date(text):
    date_starts = {start for start, _, _ in date_literals(text)}
    return any(match.end() in date_starts for match in _ESCALATION_BY_DATE.finditer(text))


def ground_situation(situation, facts, sources):
    situation.buyers.note = factual_copy.strip_buyer_note_agency_work(situation.buyers.note)
    situation.lead = source_copy.attribute_published_handover(situation.lead, facts, sources)
    for event in situation.timeline:
        event.text = source_copy.attribute_published_handover(
            event.text, facts, sources, event.refs, event_date=event.date_iso
        )
    situation = factual_copy.validate_situation(situation, facts, sources)
    for tile in situation.facts:
        tile.label = source_copy.attribute_secondary_source(tile.label, facts, sources, tile.refs)
    return situation


def ground_analysis(analysis, facts, sources, qualification):
    analysis.situation = ground_situation(analysis.situation, facts, sources)
    analysis.priority.outlook = factual_copy.name_project_deadline(analysis.priority.outlook, facts)
    for cause in analysis.diagnosis.main_problem.causes:
        cause.text = factual_copy.normalize_proven_delay_duration(cause.text, facts, cause.refs)
        cause.text = source_copy.attribute_secondary_source(cause.text, facts, sources, cause.refs)
        cause.text = normalize_forum_report_voice(cause.text, facts, sources, cause.refs)
    for sign in analysis.diagnosis.stage.signs:
        sign.text = normalize_forum_report_voice(sign.text, facts, sources, sign.refs)
        if sign.confirmed:
            sign.text = source_copy.attribute_media_stage_sign(
                sign.text, facts, sources, sign.refs
            )
    value = analysis.model_dump()
    by_n = {s["n"]: s for s in sources}
    for item in walk(value):
        for key, refs in item.items():
            if key.endswith("refs") and not set(refs) <= by_n.keys():
                raise ValueError("Ссылка на неизвестный PDF-источник")
    # Metadata, stage and labels are computed, never delegated to an author.
    if {s.n for s in analysis.diagnosis.sources} != by_n.keys():
        raise ValueError("Мониторинг источников должен сохранить каждый PDF-источник")
    for source, canonical in zip(
        sorted(analysis.diagnosis.sources, key=lambda s: s.n), metadata(sources), strict=True
    ):
        for key, val in canonical.items():
            setattr(source, key, val)
    analysis.diagnosis.sources.sort(key=lambda s: s.n)
    analysis.diagnosis.stage.current = qualification["current_stage"]
    analysis.diagnosis.stage.stages = [contract.StageLabel(**s) for s in stages_block()]
    if not any(s.confirmed and s.refs for s in analysis.diagnosis.stage.signs):
        raise ValueError("Стадия требует подтверждённого признака со ссылкой")
    for quote in analysis.situation.buyers.quotes:
        if not quote.refs or not any(quote.text in by_n[n]["text"] for n in quote.refs):
            raise ValueError("Цитата дольщика должна дословно присутствовать в своём источнике")
    factual = [analysis.situation.lead, analysis.situation.gap]
    for item in [*analysis.situation.facts, *analysis.situation.timeline]:
        if not item.refs:
            raise ValueError("Числовой факт или событие требует ссылки")
        factual.extend(
            str(v) for k, v in item.model_dump().items() if k not in {"refs", "tone", "date_iso"}
        )
    for event in analysis.situation.timeline:
        dates = [f.date for f in facts.facts if f.source_n in event.refs and f.date]
        from radar.web.report_evidence import date_span

        if not any(date_span(event.date_iso) == date_span(d) for d in dates):
            raise ValueError("Дата хронологии уточнена или отсутствует в Facts")
    analysis.situation.timeline.sort(key=lambda e: report_evidence.date_span(e.date_iso)[0])
    for point in analysis.situation.company.points:
        public_gap = "в открытых источниках" in point.text.casefold() and any(
            word in point.text.casefold() for word in ("нет", "не опубликован", "не найден")
        )
        if not point.refs and not public_gap:
            raise ValueError("Позиция компании требует ссылки")
        factual.append(point.text)
    for cause in analysis.diagnosis.main_problem.causes:
        factual.append(cause.text)
    factual.extend(topic.text for topic in analysis.situation.buyers.topics)
    if analysis.diagnosis.extra_risk:
        factual.extend(point.text for point in analysis.diagnosis.extra_risk.points)
    factual.extend(s.text for s in analysis.diagnosis.stage.signs if s.confirmed)
    # Original date precision matters; presentation differences are not new facts.
    for index, cause in enumerate(analysis.diagnosis.main_problem.causes):
        path = f"diagnosis.main_problem.causes.{index}.text"
        factual_copy.validate_copy_numbers(
            cause.text, facts, cause.refs, path=path
        )
        factual_copy.validate_construction_scope(cause.text, facts, cause.refs, path=path)
    for index, sign in enumerate(analysis.diagnosis.stage.signs):
        if sign.confirmed:
            factual_copy.validate_copy_numbers(
                sign.text, facts, sign.refs, path=f"diagnosis.stage.signs.{index}.text"
            )
    if analysis.diagnosis.extra_risk:
        for index, point in enumerate(analysis.diagnosis.extra_risk.points):
            factual_copy.validate_copy_numbers(
                point.text, facts, point.refs, path=f"diagnosis.extra_risk.points.{index}.text"
            )
    validate_serious_copy({key: value[key] for key in ("situation", "diagnosis")}, facts, sources)
    validate_authority_copy({"priority": value["priority"]}, facts, sources)
    priority = internal_priority(analysis.priority.criteria)
    analysis.priority.level = priority["level"]
    analysis.priority.label = {"high": "Высокий", "medium": "Средний", "low": "Низкий"}[
        priority["level"]
    ]
    return analysis


def ground_offer_authority(offer, facts, sources):
    issues = authority_claim_issues({"offer": offer.model_dump()}, facts, sources)
    issue_paths = {issue["path"] for issue in issues}
    neutralized = set()
    for index, item in enumerate(offer.why_now):
        path = f"offer.why_now.{index}"
        heading = path + ".title"
        if (
            heading in issue_paths and path + ".text" not in issue_paths
            and asserts_filing_heading(item.title) and is_attributed_filing(item.text)
        ):
            item.title = "Сообщения об обращениях"
            neutralized.add(heading)
    return [issue for issue in issues if issue["path"] not in neutralized]


def validate_offer(offer, context):
    by_id = {s["id"]: s for s in context["services"]}
    methods = {m["id"]: m for m in context["methods"]}
    refs = {s["n"] for s in context["sources"]}
    source_issues = []
    for index, module in enumerate(offer.modules):
        module.pain = normalize_forum_report_voice(
            module.pain, context["facts"], context["sources"], module.pain_refs
        )
        allowed_moves = {
            row["move"] for key in ("picked", "reserve") for row in context["matrix_selection"][key]
        }
        if module.move not in allowed_moves:
            raise ValueError("Ход решения отсутствует в подтверждённой подборке матрицы")
        if not set(module.service_ids) <= by_id.keys():
            raise ValueError("Услуга отсутствует в каталоге")
        if not set(module.method_ids) <= methods.keys():
            raise ValueError("Метод отсутствует в библиотеке")
        if not set(module.pain_refs) <= refs:
            raise ValueError("Боль ссылается на неизвестный источник")
        path = f"offer.modules.{index}.pain"
        factual_copy.validate_handover_scope(
            module.pain, context["facts"], module.pain_refs, path=path
        )
        factual_copy.validate_readiness_scope(
            module.pain, context["facts"], module.pain_refs, path=path
        )
        if not source_copy.forum_numbers_supported(
            module.pain, context["facts"], context["sources"], module.pain_refs
        ):
            source_issues.append({
                "path": path, "kind": "editorial_claim",
                "reason": (
                    "Даты или числа, приписанные форуму, отсутствуют в его фактах. "
                    "Сообщи подтверждённый срок со ссылкой на публикацию, а сообщение "
                    "форума передай отдельно с его собственными датами и числами."
                ),
            })
        if any(module.agency == m["steps"] for m in methods.values()):
            raise ValueError("Действия скопированы из методики без адаптации")
        if not context["public_communities"] and (
            module.service == PRODUCTS["products"]["monitoring"]
            or "S04" in module.service_ids
            or "M06" in module.method_ids
        ):
            raise ValueError("Для мониторинга нужны найденные сообщества объекта")
    if len({m.pain.casefold() for m in offer.modules}) != len(offer.modules):
        raise ValueError("Решения повторяют одну и ту же боль")
    if not usp_mentions_object(offer.usp, context):
        raise ValueError("Суть предложения должна содержать название объекта")
    for index, item in enumerate(offer.why_now):
        path = f"offer.why_now.{index}.text"
        attributed = source_copy.attribute_published_handover(
            item.text, context["facts"], context["sources"]
        )
        grounded = editorial_evidence.ground_observation_copy(
            attributed, context["facts"], repair_readiness=True
        )
        if (re.search(r"\bготовност\w*\b", grounded, re.I)
                and (grounded != attributed or _GROWING_READINESS_GAP.search(item.title))):
            item.title = "Готовность объекта"
        item.text = grounded
        factual_copy.validate_handover_scope(item.text, context["facts"], path=path)
        factual_copy.validate_readiness_scope(item.text, context["facts"], path=path)
        missing = source_copy.missing_deadline_publishers(
            item.text, context["facts"], context["sources"]
        )
        if missing:
            source_issues.append({
                "path": path, "kind": "editorial_claim",
                "reason": (
                    f"Названный срок не подтверждён в материалах {', '.join(missing)}. "
                    "Раздели публикации по содержанию: укажи срок только за сообщившим его "
                    "изданием, отдельно передай подтверждённую позицию компании."
                ),
            })
    issues = ground_offer_authority(offer, context["facts"], context["sources"])
    issues.extend(source_issues)
    issues.extend(editorial_evidence.editorial_claim_issues(
        {"offer": offer.model_dump()}, context["facts"]
    ))
    offer_outcomes = [("offer.usp", offer.usp), *(
        (f"offer.modules.{index}.outcome", module.outcome)
        for index, module in enumerate(offer.modules)
    )]
    issues.extend({
        "path": path,
        "kind": "offer_promise",
        "reason": (
            "Дата удержания стадии или предотвращения эскалации не в контроле агентства. "
            "Укажи конкретный результат его работы: согласованную позицию, порядок ответов "
            "или протокол переговоров."
        ),
    } for path, text in offer_outcomes if promises_to_limit_escalation_by_date(text))
    if issues:
        raise CopyValidationError(issues)
    offer.subtitle = PLAYBOOK["offer_subtitles"][context["qualification"]["current_stage"]]
    return offer


def focus_qualification(value, mapping):
    result = json.loads(json.dumps(value))
    research = result.get("research_evidence") or {}
    research["evidence"] = [e for e in research.get("evidence", []) if e["source_n"] in mapping]
    for node in walk(result):
        for key, val in list(node.items()):
            if key.endswith("refs") or key == "source_numbers":
                node[key] = [mapping[n] for n in val if n in mapping]
            elif key == "source_n":
                node[key] = mapping.get(val)
            elif key == "fact_indexes" or key.endswith("_fact_indexes"):
                node.pop(key)
    return result


def match_matrix(value, context):
    metadata = matrix.metadata()
    problem_ids = {p["id"] for p in context["qualification"]["problem_registry"]}
    known_matrix = {p["id"] for p in metadata["problems"]}
    matches = {
        m.problem_id: m.matrix_id
        for m in value.problems
        if m.problem_id in problem_ids and m.matrix_id in known_matrix
    }
    refs = {s["n"] for s in context["sources"]}
    signals = {}
    for key in ("tactics", "accelerators", "factors"):
        known = {row["id"] for row in metadata[key]}
        signals[key] = [
            s.model_dump() for s in getattr(value, key) if s.code in known and set(s.refs) <= refs
        ]
    return {
        "problems": [{"problem_id": p, "matrix_id": matches.get(p)} for p in sorted(problem_ids)],
        **signals,
    }


def select_matrix(matched, qualification, focus, *, has_public_communities):
    """An absent match cannot manufacture a problem to sell a diagnostic."""
    primary = focus["primary_problem"]["id"]
    problem = next((p for p in qualification["problem_registry"] if p["id"] == primary), None)
    communication_gap = bool(
        problem
        and problem.get("in_scope")
        and problem.get("status") == "active"
        and problem.get("topic") == "communication"
        and problem.get("active_dated_evidence_count", 0) > 0
    )
    by_problem = {row["problem_id"]: row["matrix_id"] for row in matched["problems"]}
    primary_refs = set(focus["primary_problem"].get("refs") or [])
    grounded = {
        kind: [row for row in matched[kind] if set(row["refs"]) & primary_refs]
        for kind in ("tactics", "accelerators", "factors")
    }
    if not by_problem.get(primary) and not communication_gap and not any(grounded.values()):
        raise NeedsReview("Главная подтверждённая проблема не сопоставлена с матрицей решений")
    selection = matrix.suggest(
        qualification["current_stage"],
        problems=[by_problem[primary]] if by_problem.get(primary) else [],
        tactics=[row["code"] for row in grounded["tactics"]],
        accelerators=[row["code"] for row in grounded["accelerators"]],
        factors=[row["code"] for row in grounded["factors"]],
        has_public_communities=has_public_communities,
        confirmed_communication_gap=communication_gap,
    )
    if len({row["move"] for key in ("picked", "reserve") for row in selection[key]}) < 2:
        raise NeedsReview("Матрица не дала двух разных решений по подтверждённой проблеме")
    return selection


class Runner:
    def __init__(self, store, run_id, client, invoke=None):
        self.store, self.run_id, self.client = store, run_id, client
        self.cfg, self.models = configuration(store.root), model_config(store.root)
        self.usage, self.calls, self.repairs = [], 0, 0
        self.copy_repairs = 0
        self.layout_repairs = 0
        self.repairs_by_stage = {}
        self.revisions, self.review_errors = [], []
        self.candidates = {}
        self.http_calls, self.transport_retries = 0, []
        self._http_invoke = invoke or call_json
        self.invoke = self._invoke_with_retries

    def _invoke_with_retries(self, client, url, headers, body, label):
        stage = label.removeprefix("Отчёт: ")

        def request():
            self.http_calls += 1
            return self._http_invoke(client, url, headers, body, label)

        def waiting(*, retry_number, delay, status_code):
            self.transport_retries.append(
                {
                    "stage": stage,
                    "retry": retry_number,
                    "wait_seconds": delay,
                    "status_code": status_code,
                }
            )
            completed = {
                "facts": 1,
                "research": 2,
                "situation": 3,
                "assessment": 3,
                "scenario": 4,
                "offer": 4,
                "critic": 5,
            }.get(stage.split(":")[0], 2)
            report_progress(
                self.store,
                self.run_id,
                completed,
                "Сервис моделей временно занят — продолжаем тот же этап",
                f"Ожидание {delay:g} сек. · повтор {retry_number}/2",
            )

        # The background worker can wait for an explicit 60-second provider
        # window while the UI remains responsive; never retry before Retry-After.
        return retry_model_call(request, on_wait=waiting, delay_cap=60, total_wait_cap=90)

    def _repair_copy(self, stage, parsed, error, model_contract, context, validator):
        """Edit all addressable failures, then re-run the complete original gate."""
        attempted = set()
        while True:
            previous = deepcopy(parsed.model_dump())
            request = copy_edit.batch_request(
                error, context["facts"], previous, sources=context.get("sources", ())
            )
            if not request:
                raise error
            edits = request["edits"]
            signature = frozenset(
                (edit["path"], edit["current_text"], edit["error"]) for edit in edits
            )
            if signature in attempted:
                raise NeedsReview(str(error)) from error
            attempted.add(signature)
            try:
                self.reserve_copy_repair(stage)
            except NeedsReview as exc:
                raise NeedsReview(str(error)) from exc
            if len(edits) == 1:
                answer = self.step(
                    "copy_edit:" + stage, copy_edit.single_contract(edits[0]), copy_edit.PROMPT,
                    edits[0], "author",
                )
                patched = copy_edit.apply(previous, edits[0], answer.text)
            else:
                answer = self.step(
                    "copy_edit:" + stage, copy_edit.batch_contract(request),
                    copy_edit.PROMPT_BATCH, request, "author",
                )
                patched = copy_edit.batch_apply(previous, request, answer)
            if patched == previous:
                raise NeedsReview(str(error)) from error
            parsed = model_contract.model_validate(patched)
            self.candidates[stage] = deepcopy(parsed.model_dump())
            for edit in edits:
                self.revisions.append({
                    "kind": "copy_edit", "page": stage, "path": edit["path"],
                    "before": edit["current_text"],
                })
            try:
                return validator(parsed)
            except (ValueError, KeyError, TypeError, IndexError) as exc:
                error = exc

    def reserve_copy_repair(self, page):
        key = "copy:" + page
        count = self.repairs_by_stage.get(key, 0)
        if count >= _COPY_REPAIRS_PER_PAGE[page]:
            raise NeedsReview("Точечные исправления страницы исчерпаны")
        self.copy_repairs += 1
        self.repairs_by_stage[key] = count + 1
        self.repairs_by_stage["copy"] = self.copy_repairs

    def repair(self, stage="editorial"):
        # A malformed matrix reply must not consume the writer's entire budget.
        if stage == "layout":
            if self.layout_repairs >= 1:
                raise NeedsReview("После сокращения текст всё ещё не помещается в макет V8")
            self.layout_repairs += 1
            self.repairs_by_stage["layout"] = self.layout_repairs
            return
        count = self.repairs_by_stage.get(stage, 0)
        if stage == "review_revision":
            if count >= 2:
                raise NeedsReview("После двух исправлений критик снова отклонил содержание")
            self.repairs_by_stage[stage] = count + 1
            return
        limit = 2 if stage in {"critic", "editorial"} else 1
        if count >= limit or self.repairs >= 6:
            raise NeedsReview("Ограниченные исправления исчерпаны на этапе " + stage)
        self.repairs_by_stage[stage] = count + 1
        self.repairs += 1

    def step(
        self,
        stage,
        model_contract,
        prompt,
        context,
        role,
        validator=lambda v: v,
        *,
        parser=None,
        parse_contract=None,
    ):
        feedback, previous = None, None
        failures, rejected_replies = set(), []
        while True:
            model = self.models[role]
            mode = model.get("response_mode", "tool" if role == "author" else "schema")
            report_progress(
                self.store,
                self.run_id,
                {
                    "facts": 1,
                    "research": 2,
                    "situation": 3,
                    "assessment": 3,
                    "scenario": 4,
                    "offer": 4,
                    "critic": 5,
                }.get(stage.split(":")[0], 2),
                {
                    "facts": "Читаем полный набор источников",
                    "research": "Проверяем паспорт и текущие проблемы",
                    "situation": "Собираем ситуацию и хронологию",
                    "assessment": "Оцениваем конфликт и источники",
                    "scenario": "Собираем сценарии и риски",
                    "offer": "Подбираем решения под проблемы",
                    "critic": "Независимо проверяем содержание",
                }.get(stage.split(":")[0], "Дорабатываем разбор"),
                "Сверяем факты и уточняем формулировки" if feedback else "",
            )

            def operation(model=model, feedback=feedback, previous=previous, mode=mode):
                self.calls += 1
                if mode not in {"prompt", "json", "schema", "tool"}:
                    raise ModelCallError("Неизвестный режим ответа модели в настройках роли")
                formatting = {}
                if mode == "schema":
                    formatting["response_format"] = structured_format(model_contract)
                elif mode == "json":
                    formatting["response_format"] = {"type": "json_object"}
                elif mode == "tool":
                    formatting["tools"] = [
                        {
                            "type": "function",
                            "function": {
                                "name": AUTHOR_TOOL,
                                "description": "Вернуть проверенные данные разбора по схеме",
                                "parameters": model_contract.model_json_schema(),
                            },
                        }
                    ]
                    formatting["tool_choice"] = {
                        "type": "function",
                        "function": {"name": AUTHOR_TOOL},
                    }
                try:
                    response = self.invoke(
                        self.client,
                        "https://openrouter.ai/api/v1/chat/completions",
                        {"Authorization": "Bearer " + self.cfg["OPENROUTER_API_KEY"]},
                        {
                            **model_options(model),
                            **formatting,
                            "provider": {"require_parameters": True},
                            "messages": [
                                {
                                    "role": "system",
                                    "content": prompts.COMMON
                                    + prompt
                                    + "\n"
                                    + json.dumps(
                                        model_contract.model_json_schema(), ensure_ascii=False
                                    ),
                                },
                                {
                                    "role": "user",
                                    "content": json.dumps(
                                        {
                                            "data": context,
                                            "feedback": feedback,
                                            "previous": previous,
                                        },
                                        ensure_ascii=False,
                                    ),
                                },
                            ],
                        },
                        "Отчёт: " + stage,
                    )
                except ValueError as exc:
                    raise ModelCallError(str(exc)) from exc
                usage = response.get("usage") if isinstance(response, dict) else None
                return {"response": response, "usage": usage if isinstance(usage, dict) else {}}

            cache_key = [
                VERSION,
                prompts.COMMON,
                stage,
                {**model, "response_mode": mode} if role == "author" else model,
                prompt,
                model_contract.model_json_schema(),
                context,
                feedback,
            ]
            raw, hit = cached(self.store, cache_key, operation, self.run_id, "report-model")
            self.usage.append(
                {"stage": stage, "model": model["model"], "cached": hit, **raw["usage"]}
            )
            parsed = None
            try:
                if parser:
                    parsed = parser(raw["response"])
                elif role == "author" and mode == "tool":
                    parsed = parse_author_tool(
                        raw["response"], parse_contract or model_contract, repair_arrays=True
                    )
                elif role == "author":
                    parsed = parse_author_json(
                        raw["response"], parse_contract or model_contract, repair_arrays=True
                    )
                else:
                    parsed = parse_model_json(
                        raw["response"], parse_contract or model_contract, repair_arrays=True
                    )
                # A rejected page is still the concrete input to its next edit.
                # Assignment to pages happens only after validation, so relying
                # on pages alone would regenerate it without the failing draft.
                self.candidates[stage] = deepcopy(
                    parsed.model_dump() if hasattr(parsed, "model_dump") else parsed
                )
                return validator(parsed)
            except (ValueError, KeyError, TypeError, IndexError) as exc:
                if isinstance(exc, review_v8.ReviewGroundingError):
                    self.review_errors.append(exc.feedback)
                custom_validation = isinstance(exc, ValidationError) and any(
                    issue["type"] in {"value_error", "assertion_error"} for issue in exc.errors()
                )
                unreadable = parsed is None and not custom_validation
                if unreadable:
                    rejected_replies.append((cache_key, raw))
                reply = raw["response"]
                choices = reply.get("choices") if isinstance(reply, dict) else None
                choice = choices[0] if isinstance(choices, list) and choices else None
                message = choice.get("message") if isinstance(choice, dict) else None
                message = message if isinstance(message, dict) else {}
                tool_calls = message.get("tool_calls")
                content = [
                    call.get("function") for call in tool_calls if isinstance(call, dict)
                ] if isinstance(tool_calls, list) else []
                content = content or message.get("content")
                try:
                    content = json.loads(content)
                except (ValueError, TypeError):
                    pass
                signature = hashlib.sha256(
                    json.dumps(
                        [str(exc), content],
                        sort_keys=True,
                        ensure_ascii=False,
                    ).encode()
                ).hexdigest()
                if signature in failures:
                    detail = "Модель повторила те же замечания: " + str(exc)
                    raise self.output_failure(detail, unreadable, rejected_replies) from exc
                failures.add(signature)
                if parsed is not None and role == "author" and stage in {
                    "situation", "assessment", "scenario", "offer"
                } and context.get("facts"):
                    if copy_edit.batch_request(exc, context["facts"], self.candidates[stage]):
                        return self._repair_copy(
                            stage, parsed, exc, parse_contract or model_contract, context, validator
                        )
                try:
                    self.repair(stage)
                except NeedsReview:
                    raise self.output_failure(str(exc), unreadable, rejected_replies) from exc
                feedback = str(exc)
                failed = self.candidates[stage] if parsed is not None else content
                if feedback.startswith(("situation.", "diagnosis.", "scenario.")) and context.get(
                    "facts"
                ):
                    feedback = factual_copy.repair_context(
                        exc, context["facts"], previous=failed
                    )
                previous = json.dumps(failed, ensure_ascii=False) if not isinstance(
                    failed, str
                ) else failed

    def output_failure(self, detail, unreadable, rejected_replies):
        if not unreadable:
            return NeedsReview(detail)
        for key, value in rejected_replies:
            discard_cached(self.store, key, value)
        return InvalidModelOutput(detail)


def _extract_sources(runner, payload, sources, issues, *, prefix="facts", all_sources=None):
    extracted = []
    for index, batch in enumerate(source_batches(sources)):
        batch = [_semantic_source(row) for row in batch]

        def filtered(value, batch_index=prefix + ":" + str(index)):
            return filter_fact_batch(
                value, all_sources or sources, issues=issues, batch=batch_index
            )

        result = runner.step(
            prefix + ":" + str(index),
            FactsBatch,
            prompts.FACTS + " В этом пакете до24 фактов: text≤220, verbatim≤400. "
            "Выбери факты о статусе, датах, причинах, претензиях и действиях сторон. "
            "Для текущего статуса сохраняй конкретное выполняемое действие: в text и "
            "verbatim должны быть его предмет и сказуемое, а не одни проценты готовности. "
            "В датированном сообщении о застройщике сохрани отдельно, кто назван "
            "застройщиком или ответственным, если это прямо сказано. "
            "Если полезных подтверждённых фактов нет, facts=[]. Не добавляй тендеры, "
            "рекламные показы и события других объектов ради заполнения списка.",
            {
                "object": {
                    k: payload.get(k)
                    for k in ("name", "developer", "region", "address", "aliases", "report_scope")
                },
                "sources": batch,
            },
            "extract",
            filtered,
            parser=lambda response, batch_index=prefix + ":" + str(index): parse_fact_batch(
                response, issues=issues, batch=batch_index
            ),
        )
        extracted.extend(result.facts)
    return extracted


def _fingerprint(payload, sources, catalog, library, models, as_of):
    # Source content already includes the research. Counters, cache hits and run
    # timestamps must not make identical evidence a new paid report.
    research = payload.get("report_research") or {}
    inputs = [
        VERSION,
        as_of,
        [_semantic_source(row) for row in sorted(sources, key=lambda row: row["n"])],
        {
            k: payload.get(k)
            for k in (
                "name",
                "developer",
                "region",
                "note",
                "topics",
                "building",
                "corpus",
                "address",
                "aliases",
                "summary",
                "check",
                "status",
                "situation",
                "stage",
                "followup",
                "monitoring_score",
                "monitoring_date",
                "monitoring_snapshot",
                "monitoring",
                "manual_monitoring",
                "report_scope",
                "passport",
            )
        },
        {k: research.get(k) for k in ("version", "signature")},
        catalog,
        library,
        models,
        template_hash(),
        contract.ReportV5.model_json_schema(),
    ]
    for module in (
        prompts,
        report_evidence,
        report_focus,
        matrix,
        research_anchors,
        pdf_evidence,
        review_v8,
    ):
        inputs.append(Path(module.__file__).read_text())
    for filename in (
        "pipeline_v8.py",
        "fact_spans.py",
        "fact_numbers.py",
        "author_output.py",
        "factual_copy.py",
        "editorial_brief.py",
        "editorial_evidence.py",
        "copy_edit.py",
        "author_output.py",
        "serious_claims.py",
        "source_copy.py",
    ):
        path = ROOT / filename
        if path.exists():
            inputs.append(path.read_text())
    for filename in (
        "report_search.py", "report_followup.py", "report_chats.py", "research_support.py",
        "model_output.py",
    ):
        inputs.append((ROOT.parent / "web" / filename).read_text())
    return hashlib.sha256(
        json.dumps(inputs, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _saved_fingerprint(store, ident, fingerprint):
    with store.connect() as db:
        for row in db.execute("SELECT * FROM briefs WHERE record_id=? ORDER BY id DESC", (ident,)):
            old = json.loads(row["payload"])
            pipeline = old.get("report_pipeline", {})
            if (pipeline.get("fingerprint") == fingerprint
                    and pipeline.get("issue_kind") != "model_output"):
                return {**dict(row), "payload": old}
    return None


def _merge_followup(store, ident, payload, result):
    """Persist one round once; duplicate URLs never renumber existing proof."""
    research = payload.setdefault("report_research", {})
    docs = research.setdefault("documents", [])
    by_url = {canonical_url(d["url"]): n for n, d in enumerate(docs) if d.get("url")}
    changed = []
    for doc in result.get("documents", []):
        if not doc.get("url"):
            continue
        url = canonical_url(doc["url"])
        if url in by_url:
            index = by_url[url]
            if docs[index].get("text") or doc.get("read_status") != "loaded" or not doc.get("text"):
                continue
            doc = {**doc, "url": docs[index]["url"]}
            docs[index] = doc
        else:
            by_url[url] = len(docs)
            docs.append(doc)
        changed.append(doc)
    rounds = research.setdefault("followup_rounds", [])
    signature = result.get("signature")
    if signature not in rounds:
        for key in ("search_calls", "page_reads", "cache_hits"):
            research[key] = int(research.get(key, 0)) + int(result.get(key, 0))
        if signature:
            rounds.append(signature)
    research["followup"] = {k: v for k, v in result.items() if k not in {"documents", "cached"}}
    # This helper is used only with the actual research adapter, not test stores.
    from radar.web.report_search import initialize

    initialize(store)
    persisted = {
        **research,
        "documents": [
            d for d in docs if not str(d.get("source_id", "")).startswith("report-chat:")
        ],
    }
    with store.connect() as db:
        db.execute(
            "INSERT INTO report_research VALUES(?,?,?) ON CONFLICT(record_id) "
            "DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",
            (ident, json.dumps(persisted, ensure_ascii=False), datetime.now(UTC).isoformat()),
        )
    return changed


def _layout_page(name):
    return {"situation": "situation", "diagnosis": "assessment", "scenario": "scenario"}.get(
        name, "offer"
    )


def _issue_pages(message):
    pages = set()
    for key, owner in (
        ("situation", "situation"),
        ("diagnosis", "assessment"),
        ("priority", "assessment"),
        ("scenario", "scenario"),
        ("offer", "offer"),
    ):
        if key in message:
            pages.add(owner)
    low = message.casefold()
    for pattern, owner in (
        (r"хронолог|цитат|ключев\w* факт|факт \[", "situation"),
        (r"решени[ея] \[|суть предложения|подзаголовок.*предлож", "offer"),
        (r"описание стадии|причина оценки|мониторинг", "assessment"),
        (r"дерево|уровень \d|сценари", "scenario"),
    ):
        if re.search(pattern, low):
            pages.add(owner)
    if not pages:
        pages = {"situation", "assessment", "scenario", "offer"}
    return pages


def page_context(context, stage):
    """Keep evidence intact while withholding offer catalogues from factual pages."""
    if stage == "offer":
        return dict(context)
    extras = {
        "situation": {"buyer_quotes"},
        "assessment": {"stage_labels", "monitoring_scale", "current_monitoring_score"},
        "scenario": {"stage_labels", "allowed_risks"},
    }
    if stage not in extras:
        raise ValueError("Неизвестная страница авторского контекста")
    fields = {
        "as_of",
        "object",
        "facts",
        "sources",
        "qualification",
        "pdf_registry_mapping",
        "focus",
        "report_scope",
        "text_rules",
        "editorial_brief",
        "evidence_roles",
    } | extras[stage]
    return {key: value for key, value in context.items() if key in fields}


def previous_page(pages, runner, page, options, *, prefer_candidate=False):
    """Supply the rejected candidate as an edit, without changing accepted data."""
    previous = runner.candidates.get(page) if prefer_candidate else pages.get(page)
    if previous is None:
        previous = pages.get(page) if prefer_candidate else runner.candidates.get(page)
    if hasattr(previous, "model_dump"):
        previous = previous.model_dump()
    previous = deepcopy(previous)
    if page == "situation" and previous:
        quotes = []
        for quote in previous["buyers"]["quotes"]:
            if "quote_key" in quote:
                quotes.append(quote)
                continue
            for option in options:
                if quote["text"] == option["text"] and quote["refs"] == option["refs"]:
                    quotes.append({"quote_key": option["quote_key"]})
                    break
        previous["buyers"]["quotes"] = quotes
    return previous


def compose(payload, analysis, scenario, offer, qualification, as_of):
    passport = qualification["passport"]
    analysis_data = analysis.model_dump()
    main_problem = analysis_data["diagnosis"]["main_problem"]
    score = main_problem["score"]
    tone = "high" if score >= 7 else "medium" if score >= 4 else "low"
    monitoring = (qualification.get("focus") or {}).get("monitoring") or {}
    main_problem.update(
        level={"high": "Высокая", "medium": "Средняя", "low": "Низкая"}[tone],
        tone=tone,
        scale_note=(
            monitoring["scale_note"] if monitoring.get("valid") else "оценка по открытым данным"
        ),
    )
    return contract.ReportV5(
        **analysis_data,
        scenario=scenario,
        offer=offer.model_dump(),
        meta={
            "schema_version": "8",
            "product": "Мегаполис Интеллидженс",
            "doc_type": "Разбор объекта",
            "date": datetime.fromisoformat(as_of).strftime("%d.%m.%Y"),
            "disclaimer": "Конфиденциально · Мегаполис",
            "layout": "compact" if qualification["verdict"] == "report_compact" else "full",
            "client_role": qualification["client_role"],
        },
        object={
            "name": passport["official_name"],
            "short_name": project_short_name(passport["official_name"]),
            "developer": passport["developer_brand"],
            "city": payload["region"],
        },
        evidence_summary=str(len(analysis.diagnosis.sources)),
    )


def _save(
    store,
    ident,
    payload,
    runner,
    fingerprint,
    qualification,
    facts,
    sources,
    attempts,
    report=None,
    binary=None,
    issues=(),
    issue_kind=None,
    overflow=(),
    extraction_issues=(),
    research_issues=(),
):
    passed = report is not None and binary is not None and not issues and not overflow
    payload = json.loads(json.dumps(payload))
    payload["report_qualification"] = qualification
    if report is not None:
        payload["report_v2"] = report.model_dump()
    else:
        payload.pop("report_v2", None)
    verdict = qualification.get("verdict", "hold")
    status = "passed" if passed else verdict if verdict in {"hold", "watch"} else "manual"
    full_page_repairs = runner.repairs + runner.repairs_by_stage.get("review_revision", 0)
    payload["report_pipeline"] = {
        "version": VERSION,
        "authoring_mode": (payload.get("editorial_revision") or {}).get(
            "authoring_mode", "automatic"
        ),
        "fingerprint": fingerprint,
        "passed": passed,
        "status": status,
        "research_evidence": qualification.get("research_evidence"),
        "facts": facts.model_dump() if facts else {"facts": []},
        "sources": sources,
        "usage": runner.usage,
        "repairs": full_page_repairs + runner.copy_repairs + runner.layout_repairs,
        "full_page_repairs": full_page_repairs,
        "copy_repairs": runner.copy_repairs,
        "layout_repairs": runner.layout_repairs,
        "repairs_by_stage": runner.repairs_by_stage,
        "revisions": runner.revisions,
        "review_errors": runner.review_errors,
        "model_http_requests": runner.http_calls,
        "transport_retries": runner.transport_retries,
        "checks": attempts,
        "issues": list(issues),
        "extraction_issues": list(extraction_issues),
        "research_issues": list(research_issues),
        "matrix_selection": qualification.get("matrix_selection"),
        "focus": qualification.get("focus"),
        "overflow": list(overflow),
    }
    if issue_kind:
        payload["report_pipeline"]["issue_kind"] = issue_kind
    payload["author"] = {
        "model": runner.models["author"]["model"],
        "review_model": runner.models["review"]["model"],
        "status": "reviewed" if passed else "manual",
        "attempts": attempts
        or [
            {
                "verdict": "manual",
                "issues": [
                    {"block": "Квалификация", "severity": "review", "reason": str(i)}
                    for i in (
                        issues
                        or qualification.get("manual_checks")
                        or qualification.get("reasons")
                        or [status]
                    )
                ],
            }
        ],
    }
    if report is not None:
        payload.setdefault("report", {})["products"] = [
            {
                "code": m.service_ids[0] if m.service_ids else m.service,
                "name": m.name,
                "rationale": m.pain,
                "tasks": m.agency,
                "result": m.outcome,
                "measurement": m.metric,
                "method_ids": m.method_ids,
                "author_source_ids": [],
                "case_ids": [],
                "matching_topics": [],
            }
            for m in report.offer.modules
        ]
        payload["cases"] = []
    from radar.web import review
    from radar.web.store import now

    # V8 keeps paths, exact quotes and criterion numbers in the brief audit.
    # The shared history consumes only its public issue fields.
    review_issues = list(attempts[-1].get("issues", [])) if attempts else []
    review_issues.extend(
        {"block": "Содержание", "severity": "blocker", "reason": str(i)} for i in issues
    )
    history_issues = [
        review.Issue(
            block=item["block"][:100],
            severity=item["severity"],
            reason=item["reason"] if 5 <= len(item["reason"].strip()) <= 600 else (
                "Разбор не прошёл автоматическую проверку. Замечания сохранены в карточке."
            ),
        )
        for item in review_issues[:30]
    ]
    review_payload = review.ReviewResult(
        verdict="passed" if passed else "manual", issues=history_issues
    ).model_dump_json()
    review.initialize(store)
    check_task_cancelled()
    saved = store.save_brief(ident, payload)
    with store.connect() as db:
        existing = db.execute(
            "SELECT 1 FROM brief_reviews WHERE brief_id=? AND kind='model' "
            "AND actor=? AND payload=?",
            (saved["id"], runner.models["review"]["model"], review_payload),
        ).fetchone()
        if not existing:
            db.execute(
                "INSERT INTO brief_reviews(brief_id,kind,actor,payload,created_at) "
                "VALUES(?,'model',?,?,?)",
                (saved["id"], runner.models["review"]["model"], review_payload, now()),
            )
    if passed:
        folder = store.root / "data/local/report-pdfs"
        write_pdf_cache(folder / f"{saved['id']}.pdf", binary)
    return saved


def generate(
    store,
    ident,
    run_id,
    client=None,
    invoke=None,
    renderer=render,
    *,
    researcher=None,
    followup_researcher=None,
    telegram=None,
):
    lock = REPORT_LOCKS.for_record(store, ident)
    if not lock.acquire(blocking=False):
        raise ValueError("Подготовка отчёта по этому объекту уже выполняется")
    owns = client is None
    try:
        if researcher is None:
            from radar.web.report_search import ensure_research

            researcher = ensure_research
            if followup_researcher is None:
                from radar.web.report_followup import ensure_followup

                followup_researcher = ensure_followup
        cfg = configuration(store.root)
        client = client or routed_client(cfg, timeout=180)
        researcher(store, ident, run_id, client=client)
        chat_read = None
        if telegram is not None:
            from radar.web.report_chats import ensure_public_chats
            from radar.web.report_search import get_research

            report_progress(store, run_id, 0, "Читаем найденные открытые чаты")
            chat_read = ensure_public_chats(
                store, ident, telegram=telegram, research=get_research(store, ident)
            )
        payload = payload_for(store, ident)
        if chat_read is not None:
            payload["report_chat_read"] = {
                k: v for k, v in chat_read.items() if k not in {"documents", "checked_at"}
            }
            docs = payload.setdefault("report_research", {}).setdefault("documents", [])
            by_identity = {d["source_id"]: d for d in docs}
            by_identity.update({d["source_id"]: d for d in chat_read["documents"]})
            payload["report_research"]["documents"] = list(by_identity.values())
        if payload["dataset"] == "demo":
            raise ValueError("Демо не отправляется в API")
        catalog, library = services(store.root)
        sources = registry_sources(payload)
        payload.setdefault("report_research", {})["source_registry_numbers"] = {
            _source_key(row): row["n"] for row in sources
        }
        models = model_config(store.root)
        if not cfg.get("OPENROUTER_API_KEY"):
            raise ValueError("Для нового разбора подключите OpenRouter")
        if models["author"]["model"] == models["review"]["model"]:
            raise ValueError("Для независимой проверки выберите другую модель проверки")
        as_of = datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat()
        fingerprint = _fingerprint(payload, sources, catalog, library, models, as_of)
        saved = _saved_fingerprint(store, ident, fingerprint)
        if saved:
            return saved
        runner = Runner(store, run_id, client, invoke)
        facts, attempts, qualification, report = None, [], {}, None
        extraction_issues = []
        research_issues = []
        try:
            if not sources:
                raise NeedsReview("Нет сохранённых материалов для квалификации объекта")
            if len(sources) > _SOURCE_LIMIT:
                raise NeedsReview("В исследовании более 40 источников: требуется отбор документов")
            if not list(source_batches(sources)):
                raise NeedsReview(
                    "Нужны прочитанные материалы: доступны только сниппеты и страницы доступа"
                )
            extracted = _extract_sources(runner, payload, sources, extraction_issues)
            unique = {json.dumps(f.model_dump(), sort_keys=True): f for f in extracted}
            if not unique:
                raise NeedsReview("В прочитанных материалах нет подтверждённых фактов об объекте")
            facts = ResearchFacts(facts=list(unique.values()))
            if followup_researcher is not None:
                report_progress(store, run_id, 1, "Проверяем пробелы и ищем недостающие материалы")
                more = followup_researcher(
                    store, ident, run_id, sources=sources, facts=facts, client=client
                )
                new_docs = _merge_followup(store, ident, payload, more)
                # Existing source IDs never shift when another document is found.
                by_url = {canonical_url(row["url"]): row["n"] for row in sources if row.get("url")}
                extra_sources = []
                for row in registry_sources({"report_research": {"documents": new_docs}}):
                    if row.get("url") and canonical_url(row["url"]) in by_url:
                        continue
                    row["n"] = max((r["n"] for r in sources), default=0) + 1
                    sources.append(row)
                    extra_sources.append(row)
                    if row.get("url"):
                        by_url[canonical_url(row["url"])] = row["n"]
                if extra_sources:
                    for fact in _extract_sources(
                        runner,
                        payload,
                        extra_sources,
                        extraction_issues,
                        prefix="facts:followup",
                        all_sources=sources,
                    ):
                        unique[json.dumps(fact.model_dump(), sort_keys=True)] = fact
                    facts = ResearchFacts(facts=list(unique.values()))
                payload["report_research"]["source_registry_numbers"].update(
                    {_source_key(row): row["n"] for row in sources}
                )
                from radar.web.report_followup import plan_gaps

                remaining = plan_gaps(
                    payload, sources, facts, today=datetime.fromisoformat(as_of).date()
                )
                more.setdefault("coverage", {}).update(
                    remaining_gaps=remaining["gaps"],
                    gaps_resolved=not bool(remaining["gaps"]),
                    fact_checked=True,
                )
                _merge_followup(store, ident, payload, more)
                fingerprint = _fingerprint(payload, sources, catalog, library, models, as_of)
                saved = _saved_fingerprint(store, ident, fingerprint)
                if saved:
                    return saved
            research_input = research_context(payload, sources, facts, as_of)
            research_input["report_scope"] = payload.get("report_scope")
            # Proof candidates need the original quotation for the existing gate.
            # Strip source bodies only after deriving those copyable fact keys.
            research_input = research_anchors.anchored_context(research_input, facts)
            research_input["sources"] = [
                {k: v for k, v in _semantic_source(s).items() if k not in {"text", "messages"}}
                for s in research_input["sources"]
            ]
            research_input["search_coverage"] = {
                k: v for k, v in (payload.get("report_research") or {}).items() if k != "documents"
            }

            def semantic_research_input(node, *, budget=False):
                # Observation metadata must not invalidate the paid research step.
                # Evidence dates/counts, complete issue records and coverage states stay.
                observation = {
                    "checked_at", "fetched_at", "created_at", "updated_at", "synced_at",
                    "cached", "signature", "followup_rounds", "report_chat_read",
                }
                calls = {"cache_hits", "search_calls", "page_reads", "total_search_calls",
                         "total_page_reads", "model_http_requests", "transport_retries"}
                if isinstance(node, dict):
                    return {
                        key: semantic_research_input(value, budget=budget or key == "budget")
                        for key, value in node.items()
                        if key not in observation and (budget or key not in calls)
                    }
                if isinstance(node, list):
                    return [semantic_research_input(value, budget=budget) for value in node]
                return node

            research_input = semantic_research_input(research_input)
            def grounded_research(value):
                start = len(research_issues)
                grounded = normalize_research(
                    research_anchors.to_research(value, facts, issues=research_issues),
                    payload,
                    sources,
                    facts,
                    as_of,
                    discard_invalid=True,
                    issues=research_issues,
                    require_dated_escalation_source=True,
                )
                discarded_signs = [
                    row for row in research_issues[start:]
                    if row.get("path", "").startswith("stage_signs/")
                ]
                highest_kept = max((s.stage for s in grounded.stage_signs), default=0)
                highest_rejected = max(
                    (
                        row.get("record", {}).get("stage", 0)
                        for row in discarded_signs if isinstance(row.get("record"), dict)
                        and type(row["record"].get("stage")) is int
                    ), default=0,
                )
                if (
                    discarded_signs and highest_rejected > highest_kept
                    and not runner.repairs_by_stage.get("research")
                ):
                    # One ordinary research repair can remove an invented count
                    # or recipient; it cannot invent evidence or assign a stage.
                    # A response with no signs and no invalid claims remains a
                    # truthful watch/hold without an extra call.
                    raise ValueError(
                        "research.stage_signs: "
                        + (
                            "все признаки отклонены. " if not grounded.stage_signs else
                            "признак более высокой стадии отклонён; сохрани подтверждённые. "
                        )
                        + "Не повышай стадию без доказательства. Исправь только "
                        "неподтверждённые утверждения по их фактам; если наблюдаемого "
                        "события нет, stage_signs=[]. Замечания: "
                        + json.dumps(
                            [{"path": r["path"], "reason": r["reason"]} for r in discarded_signs],
                            ensure_ascii=False,
                        )
                    )
                return grounded

            research = runner.step(
                "research",
                research_anchors.native_contract(facts),
                research_anchors.RESEARCH_PROMPT,
                research_input,
                "author",
                grounded_research,
                parse_contract=research_anchors.RawAnchoredResearch,
            )
            qualification = qualify(research, sources, facts, as_of)
            qualification["research_evidence"] = research.model_dump()
            qualification["research_issues"] = research_issues
            if qualification["verdict"] in {"hold", "watch"}:
                return _save(
                    store,
                    ident,
                    payload,
                    runner,
                    fingerprint,
                    qualification,
                    facts,
                    sources,
                    attempts,
                    extraction_issues=extraction_issues,
                    research_issues=research_issues,
                )
            selected, pdf_facts, mapping = select_pdf_sources(sources, facts, qualification)
            identity = {
                "name": qualification["passport"]["official_name"],
                "short_name": project_short_name(qualification["passport"]["official_name"]),
                "developer": qualification["passport"]["developer_brand"],
                "city": payload["region"],
            }
            communities = sorted(
                set(qualification["passport"].get("communities") or [])
                | {s["url"] for s in sources if s.get("url") and s["contour"] == "communities"}
            )
            context = {
                "as_of": as_of,
                "object": identity,
                "facts": pdf_facts.model_dump(),
                "sources": [
                    {
                        **row,
                        **{
                            key: source.get(key)
                            for key in (
                                "contour",
                                "kind",
                                "source_kind",
                                "evidence_date",
                                "read_status",
                            )
                        },
                    }
                    for row, source in zip(metadata(selected), selected, strict=True)
                ],
                "qualification": author_qualification(qualification, mapping),
                "pdf_registry_mapping": mapping,
                "buyer_quotes": buyer_quotes(pdf_facts),
                "stage_labels": stages_block(),
                "public_communities": communities,
                "text_rules": RULES,
                "monitoring_scale": monitoring_scale(),
                "current_monitoring_score": payload.get("monitoring_score"),
                "report_scope": payload.get("report_scope"),
            }
            focus = build_focus_context(
                focus_qualification(qualification, mapping), selected, pdf_facts, payload
            )
            if not focus["can_author"]:
                raise NeedsReview("Нет датированной главной проблемы с подтверждением вне чатов")
            context["focus"] = focus
            qualification["focus"] = focus
            matched = runner.step(
                "matrix",
                contract.MatrixMatch,
                prompts.MATRIX_MATCH,
                {**context, "metadata": matrix.metadata()},
                "author",
                lambda value: match_matrix(value, context),
            )
            by_problem = {m["problem_id"]: m["matrix_id"] for m in matched["problems"]}
            for problem in qualification["problem_registry"]:
                problem["matrix_id"] = by_problem.get(problem["id"])
            qualification["matrix_matching"] = matched
            selection = select_matrix(
                matched,
                qualification,
                focus,
                has_public_communities=bool(communities),
            )
            context["matrix_selection"] = selection
            context["matrix_cards"] = [
                matrix.card(row["move"]) for key in ("picked", "reserve") for row in selection[key]
            ]
            qualification["matrix_matching"] = matched
            qualification["matrix_selection"] = selection
            context["services"], context["methods"], context["products"] = (
                catalog,
                library,
                PRODUCTS,
            )
            stage = qualification["current_stage"]
            tree = PLAYBOOK["trees"][stage]
            context["allowed_risks"] = {
                str(n): [r["title"] for r in PLAYBOOK["risk_pools"][level["risks"]]]
                for n, level in enumerate(tree["levels"], 1)
                if n > 1
            }

            options = pdf_evidence.buyer_quote_options(
                selected,
                pdf_facts,
                allowed_source_numbers=set(focus["primary_problem"]["refs"]),
            )
            context["buyer_quotes"] = options
            context["evidence_roles"] = (qualification.get("pdf_evidence_selection") or {}).get(
                "evidence_summary"
            ) or pdf_evidence.evidence_summary(selected, pdf_facts, qualification)
            context["editorial_brief"] = editorial_brief.build_editorial_brief(
                selected,
                pdf_facts,
                qualification,
                as_of,
                coverage={
                    key: value
                    for key, value in (qualification.get("pdf_evidence_selection") or {}).items()
                    if key in {"version", "research_fact_count", "pdf_fact_count"}
                },
            )
            pages, active_page = {}, None

            def author(feedback=None, targets=None):
                nonlocal active_page
                targets = set(targets or ("situation", "assessment", "scenario", "offer"))

                def inputs(page):
                    return {
                        **page_context(context, page),
                        "revision": feedback,
                        "previous_page": previous_page(
                            pages, runner, page, options,
                            prefer_candidate=last_rejected_page == page,
                        ),
                    }

                if "situation" in targets or "situation" not in pages:
                    active_page = "situation"

                    def grounded_situation(value):
                        result = ground_situation(
                            pdf_evidence.resolve_situation(value, options), pdf_facts, selected
                        )
                        for index, tile in enumerate(result.facts):
                            if not valid_fact_value(tile.value):
                                raise ValueError(
                                    f"situation.facts.{index}.value: «{tile.value}» "
                                    "должно быть числом или датой"
                                )
                        validate_serious_copy(
                            {"situation": result.model_dump()}, pdf_facts, selected
                        )
                        return result

                    pages["situation"] = runner.step(
                        "situation",
                        pdf_evidence.situation_contract(options),
                        prompts.SITUATION + " Цитаты: выбери только quote_key из buyer_quotes. "
                        "Текст/атрибуцию/refs вставит код; самостоятельно их не пиши. "
                        "Если нет подходящей цитаты, quotes=[] и подтверждённые topics.",
                        inputs("situation"),
                        "author",
                        grounded_situation,
                    )

                def grounded_assessment(value):
                    analysis = ground_analysis(
                        contract.Analysis(situation=pages["situation"], **value.model_dump()),
                        pdf_facts,
                        selected,
                        qualification,
                    )
                    return contract.Assessment(
                        priority=analysis.priority, diagnosis=analysis.diagnosis
                    )

                if "assessment" in targets or "assessment" not in pages:
                    active_page = "assessment"
                    pages["assessment"] = runner.step(
                        "assessment",
                        contract.Assessment,
                        prompts.ASSESSMENT,
                        {**inputs("assessment"), "situation": pages["situation"].model_dump()},
                        "author",
                        grounded_assessment,
                    )
                analysis = ground_analysis(
                    contract.Analysis(
                        situation=pages["situation"], **pages["assessment"].model_dump()
                    ),
                    pdf_facts,
                    selected,
                    qualification,
                )

                def grounded_scenario(value):
                    for index, trigger in enumerate(value.triggers):
                        trigger.text = editorial_evidence.ground_observation_copy(
                            trigger.text, pdf_facts, trigger.refs,
                        )
                        factual_copy.validate_copy_numbers(
                            trigger.text,
                            pdf_facts,
                            trigger.refs,
                            path="scenario.triggers." + str(index) + ".text",
                        )
                    blocks = {"scenario": value.model_dump()}
                    validate_authority_copy(blocks, pdf_facts, selected)
                    validate_editorial_copy(blocks, pdf_facts)
                    # Validate configured choices before accepting the authored page.
                    build_scenario(
                        stage,
                        [r.model_dump() for r in value.triggers],
                        value.risk_titles,
                        value.takeaway,
                        value.subtitle,
                    )
                    return value

                if "scenario" in targets or "scenario" not in pages:
                    active_page = "scenario"
                    pages["scenario"] = runner.step(
                        "scenario",
                        contract.ScenarioInput,
                        prompts.SCENARIO,
                        {**inputs("scenario"), "analysis": analysis.model_dump()},
                        "author",
                        grounded_scenario,
                    )
                scenario_input = pages["scenario"]
                scenario = build_scenario(
                    stage,
                    [r.model_dump() for r in scenario_input.triggers],
                    scenario_input.risk_titles,
                    scenario_input.takeaway,
                    scenario_input.subtitle,
                )
                if "offer" in targets or "offer" not in pages:
                    active_page = "offer"
                    pages["offer"] = runner.step(
                        "offer",
                        contract.Offer,
                        prompts.OFFER,
                        {
                            **inputs("offer"),
                            "analysis": analysis.model_dump(),
                            "scenario": scenario,
                        },
                        "author",
                        lambda v: validate_offer(v, context),
                    )
                active_page = None
                takeaway = scenario["price"]["takeaway"]
                if (localization := _UNPROVEN_LOCALIZATION.match(takeaway)):
                    remainder = takeaway[localization.end():]
                    if remainder:
                        scenario["price"]["takeaway"] = remainder[0].upper() + remainder[1:]
                return compose(payload, analysis, scenario, pages["offer"], qualification, as_of)

            feedback, targets, last_rejected_page = None, None, None
            overflow = []
            while True:
                try:
                    report = author(feedback, targets)
                    last_rejected_page = None
                    validate_focus(report, focus)
                    qualification["priority"] = internal_priority(report.priority.criteria)
                    issues = check(report.model_dump(), extra_products=[s["name"] for s in catalog])
                    if issues:
                        raise NeedsReview("; ".join(issues))
                except InvalidModelOutput:
                    raise
                except (ValueError, KeyError, ValidationError) as exc:
                    # A page validator fails before asking any dependent writer.
                    # Global code checks identify their own paths where available.
                    targets = _issue_pages(str(exc)) if re.match(
                        r"^(situation|diagnosis|priority|scenario|offer)\.", str(exc)
                    ) else {active_page} if active_page else _issue_pages(str(exc))
                    last_rejected_page = active_page if active_page in targets else None
                    feedback = {
                        "issues": [str(exc)],
                        "pages": sorted(targets),
                        "instruction": "Исправь только указанные страницы, сохрани факты.",
                    }
                    if str(exc).startswith(("situation.", "diagnosis.", "scenario.")):
                        owner = next(iter(targets)) if len(targets) == 1 else active_page
                        feedback["copy_constraints"] = factual_copy.repair_context(
                            exc,
                            pdf_facts,
                            previous=previous_page(
                                pages, runner, owner, options,
                                prefer_candidate=active_page == owner,
                            ) if owner else None,
                        )
                    runner.revisions.append({"kind": "code", **feedback})
                    try:
                        runner.repair("editorial")
                    except NeedsReview:
                        raise NeedsReview(str(exc)) from exc
                    continue

                # Check geometry before paying to review text that would need rewriting.
                # The PDF is held in memory until independent review approves this draft.
                report_progress(store, run_id, 5, "Проверяем вёрстку PDF")
                try:
                    binary, overflow = renderer(report.model_dump())
                except ValueError as exc:
                    raise ModelCallError(str(exc)) from exc
                if overflow:
                    targets = {_layout_page(row["name"]) for row in overflow}
                    if "situation" in targets:
                        # The assessment's score and outlook also appear on the cover.
                        targets.add("assessment")
                    feedback = {
                        "issues": overflow,
                        "pages": sorted(targets),
                        "instruction": (
                            "Перепиши только указанные страницы заметно короче. "
                            "Сохрани смысл, ссылки, атрибуцию, даты и дословные цитаты. "
                            "Убери повторы и оставь одну мысль в каждом поле."
                        ),
                    }
                    runner.revisions.append({"kind": "layout", **feedback})
                    runner.repair("layout")
                    continue

                # Re-reading a hallucinated critique never changes the report or
                # consumes a writer revision. Exhausted review goes to manual.
                review_fields = review_v8.field_options(report)
                checked = runner.step(
                    "critic",
                    review_v8.native_contract(review_fields),
                    prompts.CRITIC + review_v8.PROMPT_NATIVE,
                    {
                        **review_v8.compact_review_context(report, context),
                        "report_field_options": review_fields,
                    },
                    "review",
                    lambda value, candidate=report, fields=review_fields: review_v8.resolve_native(
                        value, fields, candidate
                    ),
                )
                correction = review_v8.repair_feedback(checked, report)
                attempts.append(
                    {
                        "verdict": "passed" if checked.passed else "revise",
                        "checks": checked.model_dump()["checks"],
                        "issues": [
                            {"block": row["path"], "severity": "blocker", **row}
                            for row in correction["issues"]
                        ],
                    }
                )
                if not checked.passed:
                    if "identity" in correction["pages"]:
                        raise NeedsReview("Проверяющий обнаружил проблему паспорта объекта")
                    runner.revisions.append({"kind": "critic", **correction})
                    runner.repair("review_revision")
                    feedback, targets = correction, set(correction["pages"])
                    continue
                return _save(
                    store,
                    ident,
                    payload,
                    runner,
                    fingerprint,
                    qualification,
                    facts,
                    sources,
                    attempts,
                    report,
                    binary,
                    extraction_issues=extraction_issues,
                    research_issues=research_issues,
                )
        except (NeedsReview, ValidationError) as exc:
            qualification.setdefault("verdict", "manual")
            qualification.setdefault("manual_checks", []).append(
                "Не удалось сверить цитаты и факты с материалами объекта"
                if '"fact_errors"' in str(exc)
                else str(exc)
                if len(str(exc)) <= 220
                else "Разбор требует редакторской проверки"
            )
            return _save(
                store,
                ident,
                payload,
                runner,
                fingerprint,
                qualification,
                facts,
                sources,
                attempts,
                report,
                issues=[str(exc)],
                issue_kind="model_output" if isinstance(exc, InvalidModelOutput) else None,
                overflow=overflow if report is not None else (),
                extraction_issues=extraction_issues,
                research_issues=research_issues,
            )
    finally:
        try:
            if owns and client is not None:
                client.close()
        finally:
            lock.release()
