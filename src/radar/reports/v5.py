#!/usr/bin/env python3
"""Проверки разбора кодом: то, что нельзя доверять обещанию модели.

    python tools/checks.py gold/nametkin-tower.json

Код выхода 1 — есть нарушения. Сервис вызывает check(data) перед рендером и возвращает
нарушения модели на исправление.
"""

import json
import re
from pathlib import Path

import yaml
from pydantic import ValidationError

from radar.reports.scenario import PLAYBOOK, build_scenario
from radar.reports.schema_v5 import ReportV5

ROOT = Path(__file__).resolve().parent
RULES = yaml.safe_load((ROOT / "config" / "text_rules.yaml").read_text(encoding="utf-8"))
PRODUCTS = yaml.safe_load((ROOT / "config" / "products.yaml").read_text(encoding="utf-8"))

# (путь, максимум знаков). «*» — любой элемент списка.
LIMITS = [
    ("situation.lead", 260),
    ("situation.facts.*.value", 9),
    ("situation.facts.*.label", 65),
    ("situation.timeline.*.date", 11),
    ("situation.timeline.*.text", 85),
    ("situation.buyers.quotes.*.text", 110),
    ("situation.company.points.*.text", 130),
    ("situation.gap", 46),
    ("priority.criteria.*.why", 50),
    ("diagnosis.stage.signs.*.text", 85),
    ("diagnosis.stage.forecast", 200),
    ("diagnosis.main_problem.name", 22),
    ("diagnosis.main_problem.causes.*.text", 70),
    ("diagnosis.main_problem.forecast", 170),
    ("diagnosis.sources.*.publisher", 20),
    ("diagnosis.sources.*.date", 22),
    ("diagnosis.sources.*.says", 110),
    ("diagnosis.stakeholders.*.who", 24),
    ("diagnosis.stakeholders.*.cares", 60),
    ("diagnosis.unknowns.*.what", 24),
    ("diagnosis.unknowns.*.how", 65),
    ("scenario.levels.*.risks.*.title", 36),
    ("scenario.levels.*.risks.*.text", 70),
    ("scenario.price.takeaway", 190),
    ("offer.usp", 180),
    ("offer.usp_note", 120),
    ("offer.why_now.*.title", 30),
    ("offer.why_now.*.text", 115),
    ("offer.modules.*.name", 26),
    ("offer.modules.*.note", 80),
    ("priority.outlook", 70),
    ("offer.subtitle", 30),
    ("offer.modules.*.service", 55),
    ("offer.modules.*.pain", 100),
    ("offer.modules.*.solution", 150),
    ("offer.modules.*.agency.*", 60),
    ("offer.modules.*.developer.*", 50),
    ("offer.modules.*.variants.*.name", 18),
    ("offer.modules.*.variants.*.text", 75),
    ("offer.modules.*.outcome", 90),
    ("offer.modules.*.metric", 70),
    ("offer.pilot.columns.*.points.*", 45),
]
COUNTS = [
    ("situation.facts", 4, 4),
    ("priority.criteria", 2, 2),
    ("situation.buyers.quotes", 0, 2),
    ("diagnosis.main_problem.causes", 3, 4),
    ("diagnosis.sources", 1, 6),
    ("diagnosis.unknowns", 3, 3),
    ("diagnosis.stakeholders", 3, 4),
    ("scenario.levels", 3, 3),
    ("offer.why_now", 3, 3),
    ("offer.modules", 2, 4),
]


def walk(node, path):
    """Все значения по пути вида a.b.*.c вместе с их адресом."""
    if not path:
        yield "", node
        return
    head, rest = path[0], path[1:]
    if head == "*":
        for i, item in enumerate(node or []):
            for p, v in walk(item, rest):
                yield f"[{i}]{p}", v
    elif isinstance(node, dict) and head in node:
        for p, v in walk(node[head], rest):
            yield f".{head}{p}", v


def strings(node, path=""):
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, dict):
        for k, v in node.items():
            if k != "url":
                yield from strings(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from strings(v, f"{path}[{i}]")


def check(d: dict, extra_products=()) -> list:
    try:
        ReportV5.model_validate(d)
    except ValidationError as exc:
        return ["контракт: " + ".".join(map(str, e["loc"])) + ": " + e["msg"] for e in exc.errors()]
    errs = []
    for path, lim in LIMITS:
        for p, v in walk(d, path.split(".")):
            if isinstance(v, str) and len(v) > lim:
                errs.append(f"длина{p}: {len(v)} > {lim} — «{v[:50]}…»")
    for path, lo, hi in COUNTS:
        for p, v in walk(d, path.split(".")):
            if not lo <= len(v) <= hi:
                errs.append(f"количество{p}: {len(v)}, нужно {lo}–{hi}")

    # стоп-фразы, внутренние слова, термины; цитаты дольщиков не трогаем
    for p, s in strings(d):
        low = s.lower()
        is_quote = ".quotes[" in p and p.endswith(".text")
        if not (p.startswith(".priority.l") or p.startswith(".meta")):
            for ph in RULES["stop_phrases"]:
                if ph in low:
                    errs.append(f"стоп-фраза «{ph}» в {p}: «{s[:60]}»")
            for w in RULES["internal_words"]:
                if re.search(rf"(?<![а-яё]){re.escape(w)}(?![а-яё])", low):
                    errs.append(f"внутреннее слово «{w}» в {p}: «{s[:60]}»")
        if not is_quote:
            for bad in RULES["replace_terms"]:
                if bad in low:
                    errs.append(f"термин «{bad}» в {p}: «{s[:60]}»")
        for ab in RULES["forbidden_as_people"]:
            if ab == "ДДУ":
                # ДДУ is legitimate when it means a contract, never a group of people.
                if not re.search(r"(?:с|для|к|между|от)\s+ДДУ\b|\bДДУ\s+(?:требуют|ждут|пишут)", s):
                    continue
            if re.search(rf"(?<![А-Яа-яA-Za-z]){ab}(?![А-Яа-яA-Za-z])", s):
                errs.append(f"сокращение «{ab}» в {p}: «{s[:60]}»")
        if "xn--" in low:
            errs.append(f"punycode в {p}")

    # ключевые факты — число или дата
    pat = re.compile(RULES["fact_value_pattern"])
    for i, f in enumerate(d["situation"]["facts"]):
        if not pat.match(f["value"]):
            errs.append(f"факт [{i}] «{f['value']}»: нужно число или дата, а не слово")

    # цитаты
    for i, q in enumerate(d["situation"]["buyers"]["quotes"]):
        if (
            len(q["text"].split()) < RULES["quote"]["min_words"]
            and len(d["situation"]["buyers"]["quotes"]) == 1
        ):
            errs.append(f"цитата [{i}] слишком короткая и единственная: «{q['text']}»")
        if not q.get("refs"):
            errs.append(f"цитата [{i}] без ссылки на источник")

    # хронология по порядку
    iso = [e.get("date_iso") for e in d["situation"]["timeline"]]
    if any(x is None for x in iso):
        errs.append("хронология: у события нет date_iso")
    elif iso != sorted(iso):
        errs.append(f"хронология не по порядку: {iso}")

    # ссылки и источники
    known = {s["n"] for s in d["diagnosis"]["sources"]}
    weak = {
        s["n"]
        for s in d["diagnosis"]["sources"]
        if s["type"] in ("отзывы", "чат", "канал", "сайт дольщиков")
    }

    # серьёзные утверждения — только со ссылкой на ведомство, карточку объекта или СМИ
    def serious(node, path=""):
        if isinstance(node, dict):
            text = " ".join(
                v for k, v in node.items() if isinstance(v, str) and k in ("text", "label", "title")
            ).lower()
            refs = [r for k, v in node.items() if k.endswith("refs") for r in v]
            if (
                text
                and any(t in text for t in RULES["needs_official_or_media"])
                and "refs" in "".join(node)
            ):
                if not refs or set(refs) <= weak:
                    errs.append(
                        f"серьёзное утверждение без ссылки на СМИ или ведомство в {path}: "
                        f"«{text[:60]}»"
                    )
            for k, v in node.items():
                serious(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                serious(v, f"{path}[{i}]")

    serious(d["situation"], ".situation")
    serious(d["diagnosis"], ".diagnosis")

    def refs_of(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                if k.endswith("refs"):
                    for r in v:
                        if r not in known:
                            errs.append(f"ссылка на несуществующий источник {r} в {path}.{k}")
                else:
                    refs_of(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                refs_of(v, f"{path}[{i}]")

    refs_of(d)
    for s in d["diagnosis"]["sources"]:
        if not re.search(r"\d{4}", s["date"]):
            errs.append(f"источник {s['n']}: нет даты")
        if s["type"] not in RULES["source_types"]:
            errs.append(f"источник {s['n']}: тип «{s['type']}» не из списка")
    for i, c in enumerate(d["diagnosis"]["main_problem"]["causes"]):
        if not c.get("refs"):
            errs.append(f"причина оценки [{i}] без ссылки")
    for i, m in enumerate(d["offer"]["modules"]):
        if not m.get("pain_refs"):
            errs.append(f"решение [{i}]: боль без ссылки")
        if m["service"] not in {*PRODUCTS["products"].values(), *extra_products}:
            errs.append(f"решение [{i}]: «{m['service']}» нет в каталоге продуктов")
        blob = json.dumps(m, ensure_ascii=False).lower()
        for bad in RULES["forbidden_in_solutions"]:
            if bad in blob:
                errs.append(f"решение [{i}]: недопустимое обещание «{bad}»")
        if len(m["variants"]) != 3:
            errs.append(f"решение [{i}]: вариантов {len(m['variants'])}, нужно 3")

    # стадия и дерево — дословно из конфига
    stage = d["diagnosis"]["stage"]["current"]
    for s in d["diagnosis"]["stage"]["stages"]:
        if {k: s[k] for k in ("name", "desc")} != PLAYBOOK["stages"][s["n"]]:
            errs.append(f"описание стадии {s['n']} не совпадает с конфигом")
    if d["offer"]["subtitle"] != PLAYBOOK["offer_subtitles"][stage]:
        errs.append("подзаголовок страницы «Предложения» не из конфига")
    for s in d["diagnosis"]["sources"]:
        if s.get("count") is not None and s["count"] < RULES["monitoring_min_count_to_show"]:
            errs.append(f"источник {s['n']}: число упоминаний меньше порога — не показываем")
    sc = d["scenario"]
    triggers = sc["levels"][0]["risks"]
    for i, t in enumerate(triggers):
        if not t.get("refs"):
            errs.append(f"триггер текущей стадии [{i}] без ссылки")
    try:
        expected = build_scenario(
            stage,
            triggers,
            {
                2: [r["title"] for r in sc["levels"][1]["risks"]],
                3: [r["title"] for r in sc["levels"][2]["risks"]],
            },
            sc["price"]["takeaway"],
            sc["subtitle"],
        )
        if expected != sc:
            for a, b in zip(expected["levels"], sc["levels"], strict=True):
                if a != b:
                    errs.append(f"уровень «{b['name']}» дерева отличается от конфига")
            if expected["price"] != sc["price"] or expected["passed"] != sc["passed"]:
                errs.append("шкала сложности или строка пройденных стадий отличаются от конфига")
    except KeyError as e:
        errs.append(f"риск {e} не из набора конфига")
    return errs
