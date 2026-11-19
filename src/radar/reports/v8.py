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

from radar.reports.fact_numbers import date_literals
from radar.reports.matrix import antipattern_stems, validate_move
from radar.reports.scenario_v8 import PLAYBOOK, build_scenario
from radar.reports.schema_v8 import ReportV8
from radar.reports.serious_claims import serious_terms, without_reported_filing_authority_names

ROOT = Path(__file__).resolve().parent
RULES = yaml.safe_load((ROOT / "config_v8" / "text_rules.yaml").read_text(encoding="utf-8"))
PRODUCTS = yaml.safe_load((ROOT / "config_v8" / "products.yaml").read_text(encoding="utf-8"))

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


def valid_fact_value(value):
    if re.fullmatch(RULES["fact_value_pattern"], value) or re.fullmatch(
        r"[~≈]?\s*\d+(?:[.,]\d+)?\s*(?:тыс\.|млн|млрд)?\s*₽", value
    ):
        return True
    return any(
        start == 0 and end == len(value) and isinstance(month, int) and day is None
        for start, end, (_, month, day) in date_literals(value)
    )


def check(d: dict, extra_products=()) -> list:
    try:
        ReportV8.model_validate(d)
    except ValidationError as exc:
        return ["контракт: " + ".".join(map(str, e["loc"])) + ": " + e["msg"] for e in exc.errors()]
    errs = []
    # Copy length is an editorial target; rendered geometry decides whether it fits.
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
                if re.search(rf"(?<![а-яё]){re.escape(bad)}", low):
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
    for i, f in enumerate(d["situation"]["facts"]):
        value = f["value"]
        if not valid_fact_value(value):
            errs.append(f"факт [{i}] «{value}»: нужно число или дата, а не слово")

    # цитаты
    for i, q in enumerate(d["situation"]["buyers"]["quotes"]):
        if (
            len(q["text"].split()) < RULES["quote"]["min_words"]
            and len(d["situation"]["buyers"]["quotes"]) == 1
            and not d["situation"]["buyers"].get("topics")
        ):
            errs.append(f"цитата [{i}] слишком короткая и единственная: «{q['text']}»")
        if not q.get("refs"):
            errs.append(f"цитата [{i}] без ссылки на источник")

    # хронология по порядку
    iso = [e.get("date_iso") for e in d["situation"]["timeline"]]
    if any(x is None for x in iso):
        errs.append("хронология: у события нет date_iso")
    else:
        from radar.web.report_evidence import date_span

        spans = [date_span(value) for value in iso]
        if any(span is None for span in spans):
            errs.append("хронология: некорректная дата")
        elif [span[0] for span in spans] != sorted(span[0] for span in spans):
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
                and serious_terms(
                    without_reported_filing_authority_names(text), RULES["needs_official_or_media"]
                )
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
        for bad in {*antipattern_stems(), *RULES["forbidden_in_solutions"]}:
            if re.search(bad, blob):
                errs.append(f"решение [{i}]: недопустимое обещание «{bad}»")
        errs.extend(
            f"решение [{i}]: {issue}"
            for issue in validate_move(d["diagnosis"]["stage"]["current"], m["service"], m["move"])
        )
        if len(m["variants"]) not in {0, 3, 4, 5}:
            errs.append(f"решение [{i}]: вариантов {len(m['variants'])}, нужно 0 или 3–5")

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
