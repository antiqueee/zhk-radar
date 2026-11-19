"""Wide research → qualification → authored V5 → independent critic → checked PDF.

The source registry is deliberately wider than the six citations printed in the PDF.
No draft is downloadable and the whole run has one shared budget of two repairs.
"""

import hashlib
import json
import re
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, ValidationError

from radar.reports import prompts_v5 as prompts
from radar.reports import schema
from radar.reports import schema_v5 as contract
from radar.reports.author_output import AUTHOR_TOOL, parse_author_json, parse_author_tool
from radar.reports.fact_spans import normalized_with_spans, source_span
from radar.reports.pipeline import buyer_quotes, numbers, services, walk
from radar.reports.render import render, template_hash
from radar.reports.scenario import PLAYBOOK, build_scenario, stages_block
from radar.reports.v5 import PRODUCTS, RULES, check
from radar.web import report_evidence
from radar.web.brief_service import payload_for
from radar.web.chats import redact
from radar.web.configuration import configuration, model_config, model_options
from radar.web.discovery import call_json
from radar.web.evidence_context import sources_for
from radar.web.model_gateway import ModelCallError, routed_client
from radar.web.model_output import parse_model_json, structured_format
from radar.web.record_locks import REPORT_LOCKS
from radar.web.report_evidence import (
    RESEARCH_PROMPT,
    ResearchEvidence,
    internal_priority,
    monitoring_scale,
    normalize_research,
    normalize_sources,
    qualify,
    research_context,
)
from radar.web.sources import canonical_url, is_access_page
from radar.web.tasks import check_task_cancelled, report_progress
from radar.web.workflow import cached

VERSION = "report-kit-v5"
ROOT = Path(__file__).parent


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
    envelope = parse_model_json(response, RawFactsEnvelope)
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


def registry_sources(payload):
    """Group a whole chat/page without requiring a fabricated URL for uploaded chats."""
    grouped = {}
    for index, source in enumerate(sources_for(payload)):
        text = redact(source.get("text") or "", []).strip()
        if not text:
            continue
        url = source.get("url") or None
        if url:
            try:
                url = canonical_url(url)
            except ValueError:
                continue
        key = url or str(source.get("source_id") or source.get("chat_id") or index)
        if key not in grouped:
            grouped[key] = {**source, "url": url, "text": text}
        elif text not in grouped[key]["text"]:
            grouped[key]["text"] += "\n" + text
    result = normalize_sources([{**source, "n": n} for n, source in enumerate(grouped.values(), 1)])
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
    from radar.reports.fact_numbers import validate_fact_numbers

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
        for issue in validate_fact_numbers(fact.model_dump(), source):
            errors.append({"path": prefix, "source_n": fact.source_n, "reason": issue})
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
    """Keep passport and stage proof, then complementary current problem contours."""
    by_n = {s["n"]: s for s in sources}
    facts_by_n = {f.source_n for f in facts.facts}
    passport = qualification["passport"]
    chosen = []

    def quality(n):
        source = by_n[n]
        return (
            {"official": 4, "authorities": 4, "media": 3, "communities": 2, "reviews": 1}.get(
                source["contour"], 0
            ),
            str(source.get("evidence_date") or ""),
            n in chosen,
            len(source["text"]),
        )

    def keep_best(candidates):
        available = set(candidates) & by_n.keys() & facts_by_n
        if available:
            n = max(available, key=quality)
            if n not in chosen:
                chosen.append(n)

    # Reserve the highest current stage before passport fields can consume the slots.
    signs = qualification.get("stage_signs") or []
    current = qualification.get("current_stage") or max((s["stage"] for s in signs), default=0)
    keep_best(s["source_n"] for s in signs if s["stage"] == current)
    for key in ("status_refs", "addressee_refs", "developer_refs", "developer_status_refs"):
        candidates = passport.get(key) or []
        index_key = key.removesuffix("_refs") + "_fact_indexes"
        indexed = {
            facts.facts[i].source_n for i in passport.get(index_key, []) if i < len(facts.facts)
        }
        keep_best(set(candidates) & indexed if indexed else candidates)
    problems = [
        p
        for p in qualification.get("problem_registry", [])
        if p["in_scope"] and p["status"] == "active"
    ]
    pool = [s for s in sources if s["n"] not in chosen and s["n"] in facts_by_n]
    while pool and len(chosen) < 6:
        contours = {by_n[n]["contour"] for n in chosen}
        uncovered = [
            set(p["source_numbers"]) for p in problems if not set(p["source_numbers"]) & set(chosen)
        ]
        pool.sort(
            key=lambda s: (
                sum(s["n"] in refs for refs in uncovered),
                s["contour"] not in contours,
                quality(s["n"]),
            ),
            reverse=True,
        )
        chosen.append(pool.pop(0)["n"])
    selected, mapping = [], {}
    for n, original in enumerate(chosen, 1):
        mapping[original] = n
        selected.append({**by_n[original], "n": n, "registry_n": original})
    remapped = [
        {**f.model_dump(), "source_n": mapping[f.source_n]}
        for f in facts.facts
        if f.source_n in mapping
    ]
    return selected, ResearchFacts(facts=remapped), mapping


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


def ground_analysis(analysis, facts, sources, qualification):
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
    analysis.situation.timeline.sort(key=lambda e: e.date_iso)
    for point in analysis.situation.company.points:
        public_gap = "в открытых источниках" in point.text.casefold() and any(
            word in point.text.casefold() for word in ("нет", "не опубликован", "не найден")
        )
        if not point.refs and not public_gap:
            raise ValueError("Позиция компании требует ссылки")
        factual.append(point.text)
    for cause in analysis.diagnosis.main_problem.causes:
        factual.append(cause.text)
    factual.extend(s.text for s in analysis.diagnosis.stage.signs if s.confirmed)
    allowed = numbers(
        " ".join(f.text + " " + (f.date or "") + " " + f.verbatim for f in facts.facts)
    )
    unexpected = numbers(" ".join(factual)) - allowed
    if unexpected:
        raise ValueError("Числа и даты отсутствуют в Facts: " + ", ".join(sorted(unexpected)))
    trusted = {
        s["n"]
        for s in sources
        if s["contour"] in {"official", "media", "authorities"}
        and (s.get("evidence_date") or any(f.date for f in facts.facts if f.source_n == s["n"]))
    }
    for block in (value["situation"], value["diagnosis"]):
        for node in walk(block):
            text = " ".join(str(node.get(key) or "") for key in ("text", "label", "title", "says"))
            serious = any(stem in text.casefold() for stem in RULES["needs_official_or_media"])
            refs = {r for k, v in node.items() if k.endswith("refs") for r in v}
            if "n" in node and "publisher" in node:
                refs.add(node["n"])
            if serious and not refs & trusted:
                raise ValueError(
                    "Серьёзное утверждение требует датированного СМИ или ведомства: " + text
                )
    lead = analysis.situation.lead.casefold()
    for stem in RULES["needs_official_or_media"]:
        if stem in lead and not any(
            f.source_n in trusted and stem in (f.text + " " + f.verbatim).casefold()
            for f in facts.facts
        ):
            raise ValueError("Серьёзное утверждение в лиде не подтверждено ведомством или СМИ")
    priority = internal_priority(analysis.priority.criteria)
    analysis.priority.level = priority["level"]
    analysis.priority.label = {"high": "Высокий", "medium": "Средний", "low": "Низкий"}[
        priority["level"]
    ]
    return analysis


def validate_offer(offer, context):
    by_id = {s["id"]: s for s in context["services"]}
    methods = {m["id"]: m for m in context["methods"]}
    refs = {s["n"] for s in context["sources"]}
    if len({m.pain.casefold() for m in offer.modules}) != len(offer.modules):
        raise ValueError("Решения повторяют одну и ту же боль")
    for module in offer.modules:
        if not set(module.service_ids) <= by_id.keys():
            raise ValueError("Услуга отсутствует в каталоге")
        if not set(module.method_ids) <= methods.keys():
            raise ValueError("Метод отсутствует в библиотеке")
        if not set(module.pain_refs) <= refs:
            raise ValueError("Боль ссылается на неизвестный источник")
        if any(module.agency == m["steps"] for m in methods.values()):
            raise ValueError("Действия скопированы из методики без адаптации")
        if not context["public_communities"] and (
            module.service == PRODUCTS["products"]["monitoring"]
            or "S04" in module.service_ids
            or "M06" in module.method_ids
        ):
            raise ValueError("Для мониторинга нужны найденные сообщества объекта")
    if not usp_mentions_object(offer.usp, context):
        raise ValueError("Суть предложения должна содержать название объекта")
    offer.subtitle = PLAYBOOK["offer_subtitles"][context["qualification"]["current_stage"]]
    return offer


class Runner:
    def __init__(self, store, run_id, client, invoke=None):
        self.store, self.run_id, self.client = store, run_id, client
        self.cfg, self.models = configuration(store.root), model_config(store.root)
        self.usage, self.calls, self.repairs = [], 0, 0
        self.invoke = invoke or call_json

    def repair(self):
        if self.repairs >= 2:
            raise NeedsReview("Два исправления исчерпаны; требуется редактура аналитика")
        self.repairs += 1

    def step(
        self, stage, model_contract, prompt, context, role, validator=lambda v: v, *, parser=None
    ):
        feedback, previous = None, None
        failures = set()
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
                return {"response": response, "usage": response.get("usage", {})}

            raw, hit = cached(
                self.store,
                [
                    VERSION,
                    prompts.COMMON,
                    stage,
                    {**model, "response_mode": mode} if role == "author" else model,
                    prompt,
                    model_contract.model_json_schema(),
                    context,
                    feedback,
                ],
                operation,
                self.run_id,
                "report-model",
            )
            self.usage.append(
                {"stage": stage, "model": model["model"], "cached": hit, **raw["usage"]}
            )
            try:
                if parser:
                    parsed = parser(raw["response"])
                elif role == "author" and mode == "tool":
                    parsed = parse_author_tool(raw["response"], model_contract)
                elif role == "author":
                    parsed = parse_author_json(raw["response"], model_contract)
                else:
                    parsed = parse_model_json(raw["response"], model_contract)
                return validator(parsed)
            except (ValueError, KeyError, TypeError, IndexError) as exc:
                message = raw["response"].get("choices", [{}])[0].get("message", {})
                content = [
                    call.get("function") for call in message.get("tool_calls", [])
                ] or message.get("content")
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
                    raise NeedsReview("Модель повторила те же замечания: " + str(exc)) from exc
                failures.add(signature)
                try:
                    self.repair()
                except NeedsReview:
                    raise NeedsReview(str(exc)) from exc
                feedback = str(exc)
                previous = (
                    json.dumps(content, ensure_ascii=False)
                    if not isinstance(content, str)
                    else content
                )


def compose(payload, analysis, scenario, offer, qualification, as_of):
    passport = qualification["passport"]
    return contract.ReportV5(
        **analysis.model_dump(),
        scenario=scenario,
        offer=offer.model_dump(),
        meta={
            "schema_version": "5",
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
    payload["report_pipeline"] = {
        "version": VERSION,
        "fingerprint": fingerprint,
        "passed": passed,
        "status": status,
        "research_evidence": qualification.get("research_evidence"),
        "facts": facts.model_dump() if facts else {"facts": []},
        "sources": sources,
        "usage": runner.usage,
        "repairs": runner.repairs,
        "checks": attempts,
        "issues": list(issues),
        "extraction_issues": list(extraction_issues),
        "research_issues": list(research_issues),
        "overflow": list(overflow),
    }
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
    check_task_cancelled()
    saved = store.save_brief(ident, payload)
    from radar.web import review
    from radar.web.store import now

    review.initialize(store)
    review_issues = (attempts[-1].get("issues", []) if attempts else []) + [
        {
            "block": "Содержание",
            "severity": "blocker",
            "reason": str(i)
            if len(str(i)) <= 600
            else "Разбор не прошёл автоматическую проверку. Замечания сохранены в карточке.",
        }
        for i in issues
    ]
    # The published record and the review history describe the same version.
    review_payload = review.ReviewResult(
        verdict="passed" if passed else "manual", issues=review_issues
    ).model_dump_json()
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
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{saved['id']}.pdf").write_bytes(binary)
    return saved


def generate(store, ident, run_id, client=None, invoke=None, renderer=render, *, researcher=None):
    lock = REPORT_LOCKS.for_record(store, ident)
    if not lock.acquire(blocking=False):
        raise ValueError("Подготовка отчёта по этому объекту уже выполняется")
    owns = client is None
    try:
        if researcher is None:
            from radar.web.report_search import ensure_research

            researcher = ensure_research
        cfg = configuration(store.root)
        client = client or routed_client(cfg, timeout=180)
        researcher(store, ident, run_id, client=client)
        payload = payload_for(store, ident)
        if payload["dataset"] == "demo":
            raise ValueError("Демо не отправляется в API")
        catalog, library = services(store.root)
        sources = registry_sources(payload)
        models = model_config(store.root)
        if not cfg.get("OPENROUTER_API_KEY"):
            raise ValueError("Для нового разбора подключите OpenRouter")
        if models["author"]["model"] == models["review"]["model"]:
            raise ValueError("Для независимой проверки выберите другую модель проверки")
        as_of = datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat()
        fingerprint = hashlib.sha256(
            json.dumps(
                [
                    VERSION,
                    as_of,
                    sources,
                    payload.get("name"),
                    payload.get("developer"),
                    payload.get("region"),
                    {
                        key: payload.get(key)
                        for key in (
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
                            "passport",
                        )
                    },
                    payload.get("report_research"),
                    catalog,
                    library,
                    models,
                    template_hash(),
                    Path(prompts.__file__).read_text(),
                    Path(__file__).read_text(),
                    Path(__file__).with_name("fact_spans.py").read_text(),
                    Path(__file__).with_name("fact_numbers.py").read_text(),
                    Path(__file__).with_name("author_output.py").read_text(),
                    Path(report_evidence.__file__).read_text(),
                    contract.ReportV5.model_json_schema(),
                ],
                sort_keys=True,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        with store.connect() as db:
            for row in db.execute(
                "SELECT * FROM briefs WHERE record_id=? ORDER BY id DESC", (ident,)
            ):
                old = json.loads(row["payload"])
                if old.get("report_pipeline", {}).get("fingerprint") == fingerprint:
                    return {**dict(row), "payload": old}
        runner = Runner(store, run_id, client, invoke)
        facts, attempts, qualification, report = None, [], {}, None
        extraction_issues = []
        research_issues = []
        try:
            if not sources:
                raise NeedsReview("Нет сохранённых материалов для квалификации объекта")
            if len(sources) > 40:
                raise NeedsReview("В исследовании более 40 источников: требуется отбор документов")
            extracted = []
            batches = list(source_batches(sources))
            if not batches:
                raise NeedsReview(
                    "Нужны прочитанные материалы: доступны только сниппеты и страницы доступа"
                )
            for index, batch in enumerate(batches):

                def filtered(value, batch_index=index):
                    return filter_fact_batch(
                        value, sources, issues=extraction_issues, batch=batch_index
                    )

                result = runner.step(
                    "facts:" + str(index),
                    FactsBatch,
                    prompts.FACTS + " В этом пакете до24 фактов: text≤220, verbatim≤400. "
                    "Выбери факты о статусе, датах, причинах, претензиях и действиях сторон. "
                    "Если полезных подтверждённых фактов нет, facts=[]. Не добавляй тендеры, "
                    "рекламные показы и события других объектов ради заполнения списка.",
                    {
                        "object": {k: payload.get(k) for k in ("name", "developer", "region")},
                        "sources": batch,
                    },
                    "extract",
                    filtered,
                    parser=lambda response, batch_index=index: parse_fact_batch(
                        response, issues=extraction_issues, batch=batch_index
                    ),
                )
                extracted.extend(result.facts)
            unique = {json.dumps(f.model_dump(), sort_keys=True): f for f in extracted}
            if not unique:
                raise NeedsReview("В прочитанных материалах нет подтверждённых фактов об объекте")
            facts = ResearchFacts(facts=list(unique.values()))
            research_input = research_context(payload, sources, facts, as_of)
            research_input["sources"] = [
                {k: v for k, v in s.items() if k not in {"text", "messages"}}
                for s in research_input["sources"]
            ]
            research_input["search_coverage"] = {
                k: v for k, v in (payload.get("report_research") or {}).items() if k != "documents"
            }
            research = runner.step(
                "research",
                ResearchEvidence,
                RESEARCH_PROMPT,
                research_input,
                "author",
                lambda value: normalize_research(
                    value,
                    payload,
                    sources,
                    facts,
                    as_of,
                    discard_invalid=True,
                    issues=research_issues,
                ),
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
                "sources": metadata(selected),
                "qualification": author_qualification(qualification, mapping),
                "pdf_registry_mapping": mapping,
                "buyer_quotes": buyer_quotes(pdf_facts),
                "stage_labels": stages_block(),
                "public_communities": communities,
                "text_rules": RULES,
                "monitoring_scale": monitoring_scale(),
                "current_monitoring_score": payload.get("monitoring_score"),
            }
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

            def author(feedback=None, previous=None):
                inputs = {**context, "revision": feedback, "previous_report": previous}
                situation = runner.step(
                    "situation", contract.Situation, prompts.SITUATION, inputs, "author"
                )
                assessment = runner.step(
                    "assessment",
                    contract.Assessment,
                    prompts.ASSESSMENT,
                    {**inputs, "situation": situation.model_dump()},
                    "author",
                )
                analysis = ground_analysis(
                    contract.Analysis(situation=situation, **assessment.model_dump()),
                    pdf_facts,
                    selected,
                    qualification,
                )
                scenario_input = runner.step(
                    "scenario",
                    contract.ScenarioInput,
                    prompts.SCENARIO,
                    {**inputs, "analysis": analysis.model_dump()},
                    "author",
                )
                scenario = build_scenario(
                    stage,
                    [r.model_dump() for r in scenario_input.triggers],
                    scenario_input.risk_titles,
                    scenario_input.takeaway,
                    scenario_input.subtitle,
                )
                offer_context = {**inputs, "analysis": analysis.model_dump(), "scenario": scenario}
                offer = runner.step(
                    "offer",
                    contract.Offer,
                    prompts.OFFER,
                    offer_context,
                    "author",
                    lambda value: validate_offer(value, context),
                )
                return compose(payload, analysis, scenario, offer, qualification, as_of)

            feedback, previous = None, None
            while True:
                try:
                    report = author(feedback, previous)
                    qualification["priority"] = internal_priority(report.priority.criteria)
                    issues = check(report.model_dump(), extra_products=[s["name"] for s in catalog])
                    # Code failures are repaired before spending a review-model call.
                    if issues:
                        raise NeedsReview("; ".join(issues))
                    checked = runner.step(
                        "critic",
                        schema.Critique,
                        prompts.CRITIC,
                        {**context, "report": report.model_dump()},
                        "review",
                    )
                    attempts.append(
                        {
                            "verdict": "passed" if checked.passed else "revise",
                            "checks": checked.model_dump()["checks"],
                            "issues": [
                                {
                                    "block": str(c.criterion),
                                    "severity": "blocker",
                                    "reason": c.reason,
                                }
                                for c in checked.checks
                                if not c.passed
                            ],
                        }
                    )
                    if not checked.passed:
                        raise NeedsReview(json.dumps(attempts[-1]["issues"], ensure_ascii=False))
                    report_progress(store, run_id, 5, "Собираем PDF и проверяем страницы")
                    try:
                        binary, overflow = renderer(report.model_dump())
                    except ValueError as exc:
                        raise ModelCallError(str(exc)) from exc
                    if overflow:
                        raise NeedsReview(
                            "Переполнение макета: " + json.dumps(overflow, ensure_ascii=False)
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
                        binary,
                        extraction_issues=extraction_issues,
                        research_issues=research_issues,
                    )
                except (ValueError, KeyError, ValidationError) as exc:
                    try:
                        runner.repair()
                    except NeedsReview:
                        raise NeedsReview(str(exc)) from exc
                    feedback, previous = str(exc), report.model_dump() if report else None
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
                extraction_issues=extraction_issues,
                research_issues=research_issues,
            )
    finally:
        try:
            if owns and client is not None:
                client.close()
        finally:
            lock.release()
