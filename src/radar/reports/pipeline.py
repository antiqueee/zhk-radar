"""Facts → diagnosis → offer → approach → independent critic → bounded layout repair."""

import hashlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import yaml
from pydantic import ValidationError, create_model

from radar.briefs import methods
from radar.reports import prompts, schema
from radar.reports.render import render, template_hash
from radar.web.brief_service import payload_for
from radar.web.chats import redact
from radar.web.configuration import configuration, model_config, model_options
from radar.web.discovery import call_json
from radar.web.evidence_context import sources_for
from radar.web.model_gateway import routed_client
from radar.web.model_output import parse_model_json, structured_format
from radar.web.record_locks import REPORT_LOCKS
from radar.web.sources import canonical_url
from radar.web.tasks import check_task_cancelled, report_progress
from radar.web.workflow import cached

VERSION = "report-kit-v1"
STOP = (
    "комплексн",
    "эффективн",
    "выстроить коммуникацию",
    "наладить диалог",
    "повысить лояльность",
    "управляемый поток",
    "точки контакта",
    "в кратчайшие сроки",
    "индивидуальный подход",
)


def numbers(text):
    # Two-digit years in a full date are presentation, not a new quantity.
    text = re.sub(r"\b(\d{1,2}\.\d{1,2}\.)(\d{2})\b", r"\g<1>20\2", text)
    result = set(re.findall(r"\d+(?:[.,]\d+)?", text))
    cardinals = [
        "ноль|нуль",
        "один|одна|одно|одного|одной",
        "два|две|двух",
        "три|трех|трёх",
        "четыре|четырех|четырёх",
        "пять|пяти",
        "шесть|шести",
        "семь|семи",
        "восемь|восьми",
        "девять|девяти",
        "десять|десяти",
        "одиннадцать|одиннадцати",
        "двенадцать|двенадцати",
        "тринадцать|тринадцати",
        "четырнадцать|четырнадцати",
        "пятнадцать|пятнадцати",
        "шестнадцать|шестнадцати",
        "семнадцать|семнадцати",
        "восемнадцать|восемнадцати",
        "девятнадцать|девятнадцати",
    ]
    for value, forms in enumerate(cardinals):
        if re.search(r"\b(?:" + forms + r")\b", text, re.I):
            result.add(str(value))
    # Explicit word numerals can be displayed as digits without inventing a quantity.
    for value, stem in enumerate(
        ["перв", "втор", "трет", "четв[её]рт", "пят", "шест", "седьм", "восьм", "девят", "десят"], 1
    ):
        if re.search(r"\b" + stem + r"(?:ый|ий|ой|ого|его|ом|ем|ая|яя|ую|юю|ое|ее)\b", text, re.I):
            result.add(str(value))
    return result


def services(root):
    cfg = yaml.safe_load((root / "config/services.yaml").read_text())
    old = yaml.safe_load((root / "config" / cfg["include_offer_catalog"]).read_text())
    result = list(cfg["services"])
    result.extend(
        {"id": o["code"], "name": o["name"], "purpose": o["promise"]} for o in old["offers"]
    )
    library = methods.Library.model_validate_json(methods.LIBRARY_PATH.read_bytes())
    return result, [m.model_dump(exclude={"references"}) for m in library.methods]


def select_sources(payload):
    """Keep all quotations per URL; five cited sources, favour complementary coverage."""
    grouped = {}
    for source in sources_for(payload):
        if not source.get("url"):
            continue
        try:
            url = canonical_url(source["url"])
        except ValueError:
            continue
        clean = redact(source["text"], [])
        if url not in grouped:
            grouped[url] = {**source, "url": url, "text": clean}
        elif clean not in grouped[url]["text"]:
            grouped[url]["text"] += "\n" + clean
    pool = sorted(grouped.values(), key=lambda s: -len(s["text"]))
    chosen, hosts = [], set()
    while pool and len(chosen) < 5:
        # Prefer a new site without dropping all rich sources from one publisher.
        index = next((i for i, s in enumerate(pool) if urlsplit(s["url"]).hostname not in hosts), 0)
        source = pool.pop(index)
        hosts.add(urlsplit(source["url"]).hostname)
        chosen.append({**source, "n": len(chosen) + 1})
    if sum(len(s["text"]) for s in chosen) > 45000:
        raise ValueError("Слишком большой набор цитат: нужен отбор источников, текст не обрезан")
    return chosen


def validate_facts(facts, sources):
    by_n = {s["n"]: s for s in sources}
    for index, fact in enumerate(facts.facts):
        source = by_n.get(fact.source_n)
        if not source or not fact.verbatim.strip() or fact.verbatim not in source["text"]:
            raise ValueError("Facts.verbatim должен дословно присутствовать в указанном источнике")
        allowed = numbers(source["text"] + " " + str(source.get("published_at") or ""))
        if not numbers(fact.text + " " + (fact.date or "")) <= allowed:
            raise ValueError(
                f"Facts[{index}] добавил число/дату вне исходного материала: "
                + str(numbers(fact.text + " " + (fact.date or "")) - allowed)
            )
    return facts


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def buyer_quotes(facts):
    result = []
    for fact in facts.facts:
        if not re.search(r"покупател|дольщик|жител", fact.who_says, re.I):
            continue
        for text in re.split(r"(?<=[.!?;])\s+", fact.verbatim):
            if 15 <= len(text) <= 110 and text not in [q["text"] for q in result]:
                result.append({"text": text, "refs": [fact.source_n], "attribution": fact.who_says})
    return result[:10]


def timeline_dates(facts):
    """Display dates preserve the source's precision; summer is never turned into June."""
    result = []
    months = {
        "январ": "янв.",
        "феврал": "февр.",
        "март": "март",
        "апрел": "апр.",
        "мая": "май",
        "май": "май",
        "июн": "июнь",
        "июл": "июль",
        "август": "авг.",
        "сентябр": "сент.",
        "октябр": "окт.",
        "ноябр": "нояб.",
        "декабр": "дек.",
    }
    for fact in facts.facts:
        date = (fact.date or "").strip()
        years = list(dict.fromkeys(re.findall(r"\b(?:19|20)\d{2}\b", date)))
        if not years:
            continue
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}|\d{2}\.\d{2}\.\d{4}", date):
            label = date
        elif len(years) > 1:
            label = "–".join(years[:2])
        else:
            prefix = next((abbr for stem, abbr in months.items() if stem in date.casefold()), "")
            if not prefix:
                prefix = next(
                    (
                        word
                        for stem, word in [
                            ("лет", "лето"),
                            ("зим", "зима"),
                            ("весн", "весна"),
                            ("осен", "осень"),
                        ]
                        if stem in date.casefold()
                    ),
                    "",
                )
            label = (prefix + " " + years[0]).strip()
        if len(label) <= 10 and label not in result:
            result.append(label)
    return result


def ground_analysis(analysis, facts, sources):
    value = analysis.model_dump()
    by_n = {s["n"]: s for s in sources}
    if [s.n for s in analysis.diagnosis.sources] != list(by_n):
        raise ValueError("Список источников должен сохранить все номера и порядок")
    for source in analysis.diagnosis.sources:
        if source.url != by_n[source.n]["url"]:
            raise ValueError("Изменён URL источника")
    for item in walk(value):
        if "refs" in item and not set(item["refs"]) <= by_n.keys():
            raise ValueError("Ссылка на несуществующий источник")
    allowed = numbers(
        " ".join(f.text + " " + (f.date or "") + " " + f.verbatim for f in facts.facts)
    )
    factual = [analysis.situation.lead, analysis.situation.gap]
    for item in [*analysis.situation.facts, *analysis.situation.timeline]:
        if not item.refs:
            raise ValueError("Факт и событие хронологии требуют ссылки")
        factual.extend(str(v) for k, v in item.model_dump().items() if k not in {"refs", "tone"})
    for quote in analysis.situation.buyers.quotes:
        if not quote.refs or not any(quote.text in by_n[n]["text"] for n in quote.refs):
            raise ValueError(
                "Цитата покупателя должна быть дословной, с верной ссылкой. "
                "Выбери из buyer_quotes или оставь quotes пустым, без пересказа: " + quote.text
            )
        factual.extend([quote.text, quote.attribution])
    for point in analysis.situation.company.points:
        factual.append(point.text)
        if not point.refs and not any(
            w in point.text.casefold() for w in ("не найден", "нет", "не обнаруж")
        ):
            raise ValueError("Позиция компании требует ссылки либо обозначения отсутствия ответа")
    for driver in analysis.diagnosis.drivers:
        factual.extend([driver.title, driver.text])
        if driver.confidence != "hypothesis" and not driver.refs:
            raise ValueError("Драйвер без ссылки может быть только гипотезой")
    for sign in analysis.diagnosis.stage.signs:
        factual.append(sign.text)
        if sign.confirmed and not sign.refs:
            raise ValueError("Подтверждённый признак стадии требует ссылки")
    unexpected = numbers(" ".join(factual)) - allowed
    if unexpected:
        raise ValueError("Числа страниц 1–2 отсутствуют в Facts: " + ", ".join(sorted(unexpected)))
    criteria = analysis.priority.criteria
    if {c.name for c in criteria} != {"Острота", "Публичность", "Окно для захода"}:
        raise ValueError("Повтор критерия приоритета")
    score = sum(c.score for c in criteria)
    analysis.priority.level = "high" if score >= 8 else "medium" if score >= 5 else "low"
    analysis.priority.label = {"high": "Высокий", "medium": "Средний", "low": "Низкий"}[
        analysis.priority.level
    ]
    if not any(s.confirmed for s in analysis.diagnosis.stage.signs):
        analysis.diagnosis.stage.current = 1
        analysis.diagnosis.stage.confidence = "Недостаточно данных для стадии"
    return analysis


def validate_offer(offer, context):
    services_by_id = {s["id"]: s for s in context["services"]}
    methods_by_id = {m["id"]: m for m in context["methods"]}
    refs = {s["n"] for s in context["sources"]}

    def normalize_name(value):
        value = re.sub(r'[«»„“”"]', "", value).casefold().replace("ё", "е")
        return re.sub(r"\s+", " ", re.sub(r"^жк\s+", "", value)).strip()

    if not any(
        re.search(r"\b" + re.escape(normalize_name(name)) + r"\b", normalize_name(offer.usp))
        for name in (context["object"]["name"], context["object"]["short_name"])
    ):
        raise ValueError("УТП должно содержать название объекта")
    if len({m.pain for m in offer.modules}) != 3:
        raise ValueError("Повтор боли в трёх решениях")
    for module in offer.modules:
        if not set(module.service_ids) <= services_by_id.keys():
            raise ValueError("Услуга отсутствует в каталоге")
        if not set(module.method_ids) <= methods_by_id.keys():
            raise ValueError("Метод отсутствует в библиотеке")
        if not set(module.pain_refs) <= refs:
            raise ValueError("Боль ссылается на неизвестный источник")
        if any(module.steps == m["steps"] for m in methods_by_id.values()):
            raise ValueError("Шаги скопированы из методики, нужна адаптация к ЖК")
        if not context["public_communities"] and (
            "S04" in module.service_ids
            or "M06" in module.method_ids
            or "мониторинг чат" in module.service.casefold()
        ):
            raise ValueError(
                "Мониторинг чатов: открытые сообщества не найдены, выбери другую услугу"
            )
    check_copy(offer.model_dump())
    return offer


def offer_contract(context):
    refs = tuple(s["n"] for s in context["sources"])
    module = create_model(
        "GroundedModule", __base__=schema.Module, pain_refs=(schema.Items(Literal[refs], 1, 5), ...)
    )
    return create_model(
        "GroundedOffer", __base__=schema.Offer, modules=(schema.Items(module, 3), ...)
    )


def check_copy(value):
    text = json.dumps(value, ensure_ascii=False).casefold()
    bad = [w for w in STOP if w in text]
    if bad:
        raise ValueError("Заменить общие формулировки: " + ", ".join(bad))
    if redact(text, []) != text:
        raise ValueError("Убрать персональные контакты из коммерческого текста")


def validate_approach(approach):
    check_copy(approach.model_dump())
    if re.search(r"проблем|кризис|скандал", " ".join(approach.message.paragraphs).casefold()):
        raise ValueError("Первое сообщение должно быть нейтральным, без оценок компании")
    return approach


class Runner:
    def __init__(self, store, run_id, client, invoke=None):
        self.store, self.run_id, self.client = store, run_id, client
        self.cfg, self.models = configuration(store.root), model_config(store.root)
        self.usage, self.calls = [], 0
        self.invoke = invoke or call_json

    def parse_fitted(self, raw, contract, stage):
        """Repair only overlong fields; keep all other generated fields byte-for-byte."""
        response = raw
        for turn in range(3):
            try:
                return parse_model_json(response, contract)
            except ValidationError as exc:
                errors = exc.errors()
                if turn == 2 or any(e["type"] != "string_too_long" for e in errors):
                    raise
                content = response["choices"][0]["message"]["content"]
                fenced = re.fullmatch(r"\s*```(?:json)?\s*\n(.*?)\n```\s*", content, re.S)
                data = json.loads(fenced[1] if fenced else content)
                fields = [
                    {
                        "path": "/".join(map(str, e["loc"])),
                        "text": e["input"],
                        "max_chars": max(5, int(e["ctx"]["max_length"] * 0.65)),
                        "current_chars": len(e["input"]),
                    }
                    for e in errors
                ]
                edits = self.step(
                    stage + ":fit-text",
                    schema.TextEdits,
                    "Сократи каждое поле до max_chars, включая пробелы. Оставь запас 10%. "
                    "Сохрани смысл, числа, условия и атрибуцию. Не обрывай фразы. "
                    "Верни точно те же path, по одному исправлению на поле.",
                    {"fields": fields},
                    "extract",
                )
                by_path = {e.path: e.text for e in edits.edits}
                if len(by_path) != len(edits.edits) or set(by_path) != {f["path"] for f in fields}:
                    raise ValueError("Редактор изменил список полей для сокращения") from None
                for error in errors:
                    target = data
                    for key in error["loc"][:-1]:
                        target = target[key]
                    target[error["loc"][-1]] = by_path["/".join(map(str, error["loc"]))]
                response = {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": json.dumps(data, ensure_ascii=False)},
                        }
                    ]
                }

    def step(self, stage, contract, prompt, context, role, validator=lambda v: v):
        base = stage.split(":")[0].removeprefix("fit-").removesuffix("-repair")
        completed, label = {
            "facts": (0, "Читаем источники и выделяем факты"),
            "diagnosis": (1, "Разбираем ситуацию и хронологию"),
            "analysis": (1, "Уточняем ситуацию и хронологию"),
            "offer": (2, "Готовим предложения для объекта"),
            "approach": (3, "Готовим план действий и первый контакт"),
            "critic": (5, "Проверяем содержание отчёта"),
        }.get(base, (0, "Готовим отчёт"))
        detail = "Сокращаем текст под макет" if "fit" in stage else ""
        if "repair" in stage:
            detail = "Уточняем текст после проверки"
        report_progress(self.store, self.run_id, completed, label, detail)
        if contract is schema.Offer:
            contract = offer_contract(context)
        # Separate page contracts stay below the provider's grammar complexity limit.
        if contract is schema.Analysis:
            feedback = None
            for _ in range(3):
                inputs = {**context, "validation_feedback": feedback}
                dates = context.get("timeline_dates", [])
                event = create_model(
                    "GroundedEvent",
                    __base__=schema.Event,
                    date=(Literal[tuple(dates or ["Без даты"])], ...),
                )
                situation_contract = create_model(
                    "GroundedSituation",
                    __base__=schema.Situation,
                    timeline=(schema.Items(event, 0, 6), ...),
                )
                situation = self.step(
                    stage + ":situation",
                    situation_contract,
                    prompt + " Верни только Situation по схеме.",
                    inputs,
                    role,
                )
                assessment = self.step(
                    stage + ":diagnosis",
                    schema.Assessment,
                    prompt + " Верни только priority и diagnosis по схеме.",
                    {**inputs, "situation": situation.model_dump()},
                    role,
                )
                try:
                    return validator(
                        schema.Analysis(situation=situation.model_dump(), **assessment.model_dump())
                    )
                except ValueError as exc:
                    feedback = str(exc)
                    logging.getLogger(__name__).warning("Report grounding: %s", feedback)
            raise ValueError("Диагноз не прошёл проверку: " + feedback)
        model = self.models[role]
        feedback = None
        previous = None
        for attempt in range(3):
            if attempt:
                report_progress(
                    self.store,
                    self.run_id,
                    completed,
                    label,
                    f"Исправляем замечания: попытка {attempt + 1} из 3",
                )

            def operation(feedback=feedback, previous=previous):
                self.calls += 1
                response = self.invoke(
                    self.client,
                    "https://openrouter.ai/api/v1/chat/completions",
                    {"Authorization": "Bearer " + self.cfg["OPENROUTER_API_KEY"]},
                    {
                        **model_options(model),
                        "response_format": structured_format(contract),
                        "provider": {"require_parameters": True},
                        "messages": [
                            {
                                "role": "system",
                                "content": prompts.COMMON
                                + prompt
                                + "\nСхема с лимитами:\n"
                                + json.dumps(contract.model_json_schema(), ensure_ascii=False),
                            },
                            {
                                "role": "user",
                                "content": json.dumps(
                                    {"data": context, "feedback": feedback, "previous": previous},
                                    ensure_ascii=False,
                                ),
                            },
                        ],
                    },
                    "Отчёт: " + stage,
                )
                return {"response": response, "usage": response.get("usage", {})}

            raw, hit = cached(
                self.store,
                [
                    VERSION,
                    prompts.COMMON,
                    stage,
                    model,
                    prompt,
                    contract.model_json_schema(),
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
                return validator(self.parse_fitted(raw["response"], contract, stage))
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                feedback = str(exc)
                previous = raw["response"].get("choices", [{}])[0].get("message", {}).get("content")
                if attempt == 2:
                    raise ValueError(
                        f"Этап «{stage}» не прошёл проверку после двух исправлений: {feedback}"
                    ) from None


def compose(payload, sources, analysis, offer, approach):
    supported = [d for d in analysis.diagnosis.drivers if d.refs and d.confidence != "hypothesis"]
    compact = len(sources) < 2 or len(supported) < 3
    chats = (payload.get("chat_stats") or {}).get("messages_30d", 0)
    return schema.Report(
        **analysis.model_dump(),
        offer=offer,
        approach=approach,
        meta=schema.Meta(
            product="Мегаполис Интеллидженс",
            doc_type="Разбор объекта",
            date=datetime.now(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y"),
            disclaimer="Внутренний разбор · выводы требуют проверки",
            layout="compact" if compact else "full",
        ),
        object=schema.Object(
            name=payload["name"],
            short_name=payload["name"],
            developer=payload["developer"],
            city=payload["region"],
        ),
        evidence_summary=f"{len(sources)} источн. · "
        + (f"{chats} сообщ. за 30 дней" if chats else "чаты не прочитаны"),
    )


def save_downloadable_draft(store, ident, run_id, payload, report, models, binary, overflow):
    """Publish a clearly labelled, complete draft before optional model revisions."""
    if overflow:
        return None
    payload = json.loads(json.dumps(payload))
    payload["report_v2"] = report.model_dump()
    payload["report_pipeline"] = {
        "version": VERSION,
        "passed": False,
        "status": "draft",
        "overflow": [],
    }
    payload["author"] = {
        "model": models["author"]["model"],
        "review_model": models["review"]["model"],
        "status": "manual",
        "attempts": [
            {
                "verdict": "manual",
                "issues": [
                    {
                        "block": "Содержание",
                        "severity": "review",
                        "reason": "Черновик готов. Проверка содержания и ссылок ещё не завершена.",
                    }
                ],
            }
        ],
    }
    payload["report"]["products"] = [
        {
            "code": m.service_ids[0],
            "name": m.name,
            "rationale": m.pain,
            "tasks": m.steps,
            "result": m.outcome,
            "measurement": m.metric,
            "author_source_ids": [],
            "case_ids": [],
            "method_ids": m.method_ids,
            "matching_topics": [],
        }
        for m in report.offer.modules
    ]
    payload["cases"] = []
    check_task_cancelled()
    saved = store.save_brief(ident, payload)
    folder = store.root / "data/local/report-pdfs"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{saved['id']}.pdf").write_bytes(binary)
    with store.connect() as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='object_tasks'").fetchone():
            db.execute(
                "UPDATE object_tasks SET result=? WHERE id=? AND status='running'",
                (json.dumps(saved), run_id),
            )
    return saved


def generate(store, ident, run_id, client=None, invoke=None, renderer=render):
    lock = REPORT_LOCKS.for_record(store, ident)
    if not lock.acquire(blocking=False):
        raise ValueError("Подготовка отчёта по этому объекту уже выполняется")
    owns = client is None
    draft = None
    try:
        payload = payload_for(store, ident)
        if payload["dataset"] == "demo":
            raise ValueError("Демо не отправляется в API")
        catalog, library = services(store.root)
        sources = select_sources(payload)
        if not sources:
            raise ValueError("Для разбора нужны сохранённые источники со ссылками")
        models = model_config(store.root)
        cfg = configuration(store.root)
        if not cfg.get("OPENROUTER_API_KEY"):
            raise ValueError("Для нового разбора подключите OpenRouter")
        if models["author"]["model"] == models["review"]["model"]:
            raise ValueError("Для независимой проверки выберите другую модель проверки")
        base_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        fingerprint = hashlib.sha256(
            json.dumps(
                [
                    VERSION,
                    base_hash,
                    catalog,
                    library,
                    models,
                    template_hash(),
                    hashlib.sha256(Path(prompts.__file__).read_bytes()).hexdigest(),
                    schema.Report.model_json_schema(),
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        with store.connect() as db:
            for row in db.execute(
                "SELECT * FROM briefs WHERE record_id=? ORDER BY id DESC", (ident,)
            ):
                saved = json.loads(row["payload"])
                if saved.get("report_pipeline", {}).get("fingerprint") == fingerprint:
                    return {**dict(row), "payload": saved}
        if client is None:
            client = routed_client(cfg, timeout=180)
        runner = Runner(store, run_id, client, invoke)
        identity = {
            "name": payload["name"],
            "short_name": re.sub(r'[«»„“”"]', "", payload["name"]),
            "developer": payload["developer"],
            "city": payload["region"],
        }
        # Public community URLs only, never names or profiles of individual participants.
        communities = sorted(
            {
                s["url"]
                for s in sources
                if urlsplit(s["url"]).hostname in {"t.me", "vk.com", "max.ru"}
            }
        )
        with store.connect() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='telegram_links'").fetchone():
                communities = sorted(
                    set(communities)
                    | {
                        r[0]
                        for r in db.execute(
                            "SELECT url FROM telegram_links WHERE record_id=?", (ident,)
                        )
                    }
                )
        facts = runner.step(
            "facts",
            schema.Facts,
            prompts.FACTS,
            {"object": identity, "sources": sources},
            "extract",
            lambda f: validate_facts(f, sources),
        )
        source_meta = [
            {
                "n": s["n"],
                "url": s["url"],
                "publisher": urlsplit(s["url"]).hostname,
                "date": str(s.get("published_at") or "Дата не указана"),
                "title": "Материал об объекте",
                "type": s["kind"],
            }
            for s in sources
        ]
        context = {
            "as_of": datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat(),
            "object": identity,
            "facts": facts.model_dump(),
            "sources": source_meta,
            "chats_read": bool((payload.get("chat_stats") or {}).get("messages_30d")),
            "public_communities": communities,
            "buyer_quotes": buyer_quotes(facts),
            "timeline_dates": timeline_dates(facts),
        }
        analysis = runner.step(
            "diagnosis",
            schema.Analysis,
            prompts.ANALYSIS,
            context,
            "author",
            lambda a: ground_analysis(a, facts, sources),
        )
        proposal_context = {
            **context,
            "analysis": analysis.model_dump(),
            "services": catalog,
            "methods": library,
        }
        offer = runner.step(
            "offer",
            schema.Offer,
            prompts.OFFER,
            proposal_context,
            "author",
            lambda o: validate_offer(o, proposal_context),
        )
        approach = runner.step(
            "approach",
            schema.Approach,
            prompts.APPROACH,
            {**context, "analysis": analysis.model_dump(), "offer": offer.model_dump()},
            "author",
            validate_approach,
        )
        attempts = []

        def critique(report):
            result = runner.step(
                "critic",
                schema.Critique,
                prompts.CRITIC,
                {**proposal_context, "report": report.model_dump()},
                "review",
            )
            attempts.append(
                {
                    "verdict": "passed" if result.passed else "revise",
                    "checks": result.model_dump()["checks"],
                    "issues": [
                        {"block": str(c.criterion), "severity": "blocker", "reason": c.reason}
                        for c in result.checks
                        if not c.passed
                    ],
                }
            )
            return result

        report = compose(payload, sources, analysis, offer, approach)
        preview = report.model_copy(deep=True)
        preview.meta.disclaimer = "Внутренний черновик · проверка содержания не завершена"
        report_progress(store, run_id, 4, "Собираем PDF и проверяем вёрстку")
        binary, overflow = renderer(preview.model_dump())
        draft = save_downloadable_draft(
            store, ident, run_id, payload, preview, models, binary, overflow
        )
        checked = critique(report)
        for _repair in range(2):
            if checked.passed:
                break
            feedback = checked.model_dump()
            if any(not c.passed and c.criterion in {2, 8} for c in checked.checks):
                analysis = runner.step(
                    "diagnosis-repair",
                    schema.Analysis,
                    prompts.ANALYSIS,
                    {**context, "previous": analysis.model_dump(), "criticism": feedback},
                    "author",
                    lambda a: ground_analysis(a, facts, sources),
                )
            proposal_context["analysis"] = analysis.model_dump()
            offer = runner.step(
                "offer-repair",
                schema.Offer,
                prompts.OFFER,
                {**proposal_context, "previous": offer.model_dump(), "criticism": feedback},
                "author",
                lambda o: validate_offer(o, proposal_context),
            )
            approach = runner.step(
                "approach-repair",
                schema.Approach,
                prompts.APPROACH,
                {
                    **context,
                    "analysis": analysis.model_dump(),
                    "offer": offer.model_dump(),
                    "previous": approach.model_dump(),
                    "criticism": feedback,
                },
                "author",
                validate_approach,
            )
            report = compose(payload, sources, analysis, offer, approach)
            checked = critique(report)
        binary, overflow = renderer(report.model_dump())
        fit_attempts = []
        for _repair in range(2):
            if not overflow:
                break
            fit_attempts.append(overflow)
            pages = {item["name"] for item in overflow}
            instruction = (
                "Сократи только блоки из overflow. Не уменьшай шрифт, не обрывай фразы. "
                "Сохрани смысл, факты, ссылки и ограничения схемы."
            )
            if pages & {"situation", "diagnosis"}:
                analysis = runner.step(
                    "fit-analysis",
                    schema.Analysis,
                    prompts.ANALYSIS + instruction,
                    {**context, "previous": analysis.model_dump(), "overflow": overflow},
                    "extract",
                    lambda a: ground_analysis(a, facts, sources),
                )
                proposal_context["analysis"] = analysis.model_dump()
            if "offer" in pages:
                offer = runner.step(
                    "fit-offer",
                    schema.Offer,
                    prompts.OFFER + instruction,
                    {**proposal_context, "previous": offer.model_dump(), "overflow": overflow},
                    "extract",
                    lambda o: validate_offer(o, proposal_context),
                )
            if "approach" in pages:
                approach = runner.step(
                    "fit-approach",
                    schema.Approach,
                    prompts.APPROACH + instruction,
                    {
                        **context,
                        "offer": offer.model_dump(),
                        "previous": approach.model_dump(),
                        "overflow": overflow,
                    },
                    "extract",
                    validate_approach,
                )
            report = compose(payload, sources, analysis, offer, approach)
            checked = critique(report)
            binary, overflow = renderer(report.model_dump())
        passed = checked.passed and not overflow
        if not passed:
            report.meta.disclaimer = "Внутренний черновик · нужна проверка аналитика"
            binary, overflow = renderer(report.model_dump())
        payload["report_v2"] = report.model_dump()
        payload["report_pipeline"] = {
            "fingerprint": fingerprint,
            "version": VERSION,
            "facts": facts.model_dump(),
            "sources": source_meta,
            "usage": runner.usage,
            "overflow": overflow,
            "fit_attempts": fit_attempts,
            "checks": checked.model_dump(),
            "passed": passed,
        }
        payload["author"] = {
            "model": models["author"]["model"],
            "review_model": models["review"]["model"],
            "base_fingerprint": base_hash,
            "status": "reviewed" if passed else "manual",
            "attempts": attempts,
            "meaning": "Внутренний разбор; перед контактом проверка аналитика",
        }
        # Keep approved-client projection compatible, using the SAME newly authored content.
        payload["report"]["products"] = [
            {
                "code": m.service_ids[0],
                "name": m.name,
                "rationale": m.pain,
                "tasks": m.steps,
                "result": m.outcome,
                "measurement": m.metric,
                "author_source_ids": [],
                "case_ids": [],
                "method_ids": m.method_ids,
                "matching_topics": [],
            }
            for m in offer.modules
        ]
        payload["cases"] = []
        if overflow:
            payload["author"]["attempts"][-1]["issues"].append(
                {
                    "block": "Вёрстка",
                    "severity": "blocker",
                    "reason": "Переполнение после двух сокращений; требуется ручная редактура",
                }
            )
        check_task_cancelled()
        saved = store.save_brief(ident, payload)
        from radar.web import review
        from radar.web.store import now

        review.initialize(store)
        review_result = review.ReviewResult(
            verdict="passed" if passed else "manual",
            issues=attempts[-1]["issues"],
        )
        with store.connect() as db:
            db.execute(
                "INSERT INTO brief_reviews(brief_id,kind,actor,payload,created_at) "
                "VALUES(?,'model',?,?,?)",
                (saved["id"], models["review"]["model"], review_result.model_dump_json(), now()),
            )
        folder = store.root / "data/local/report-pdfs"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{saved['id']}.pdf").write_bytes(binary)
        if not passed:
            from radar.web.automation import notice

            notice(
                store,
                "report:" + str(saved["id"]),
                "Отчёт требует проверки аналитика: " + payload["name"],
            )
        return saved
    except ValueError:
        if draft is None:
            raise
        logging.getLogger(__name__).warning(
            "Доработка отчёта не завершена; сохранён черновик PDF %s", draft["id"], exc_info=True
        )
        return draft
    finally:
        try:
            if owns and client is not None:
                client.close()
        finally:
            lock.release()
