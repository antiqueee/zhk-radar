#!/usr/bin/env python3
"""Матрица решений: загрузка, проверка и подбор ходов для страницы «Решения».

    python tools/matrix.py validate
    python tools/matrix.py suggest --stage 2 --problems Q02 Q14 Q20 --tactics P21 P36
    python tools/matrix.py card A03
    python tools/matrix.py stats

Сервис вызывает suggest() после того, как собран реестр проблем объекта и определена стадия,
и передаёт модели кандидатов: модель выбирает из них, подставляет факты объекта в поле pain
и уточняет формулировки. Придумывать ходы вне матрицы модель не должна.
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
MDIR = ROOT / "matrix_data"


def _load(name):
    return yaml.safe_load((MDIR / name).read_text(encoding="utf-8"))


def load_matrix() -> dict:
    """Все части матрицы одним словарём; списки превращены в словари по id."""
    moves, levers = [], {}
    for f in sorted(MDIR.glob("moves_*.yaml")):
        part = yaml.safe_load(f.read_text(encoding="utf-8"))
        moves += part["moves"]
        levers.update(part.get("levers", {}))
    tact = _load("tactics.yaml")
    ctx = _load("context.yaml")
    cases = _load("cases.yaml")
    return {
        "levers": levers,
        "moves": {m["id"]: m for m in moves},
        "families": tact["families"],
        "responses": tact["responses"],
        "tactics": {t["id"]: t for t in tact["tactics"]},
        "antipatterns": {a["id"]: a for a in _load("antipatterns.yaml")["antipatterns"]},
        "problems": {q["id"]: q for q in _load("problems.yaml")["problems"]},
        "types": {t["id"]: t for t in ctx["types"]},
        "accelerators": {a["id"]: a for a in ctx["accelerators"]},
        "factors": {f["id"]: f for f in ctx["factors"]},
        "regions": cases["regions"],
        "cases": {c["id"]: c for c in cases["cases"]},
    }


MATRIX = load_matrix()
PRODUCTS = yaml.safe_load((ROOT / "config" / "products.yaml").read_text(encoding="utf-8"))
RULES = yaml.safe_load((ROOT / "config" / "text_rules.yaml").read_text(encoding="utf-8"))

# Какие рычаги уместны на какой стадии: ход с таким рычагом получает небольшой бонус.
STAGE_LEVERS = {
    1: ["transparency", "safeguards", "monitoring"],
    2: ["transparency", "participation", "inclusion", "monitoring"],
    3: ["recognition", "inclusion", "redesign", "external", "crisis"],
    4: ["crisis", "narrative", "external", "inclusion"],
}
# Лимиты карточки — те же, что у offer.modules в tools/checks.py.
CARD_LIMITS = {"name": 26, "solution": 150, "outcome": 90, "metric": 70, "world_note": 80}
CARD_FIELDS = (
    "name",
    "solution",
    "agency",
    "developer",
    "variants",
    "outcome",
    "metric",
    "world_note",
)


def antipattern_stems() -> list:
    """Шаблоны, недопустимые в тексте решения: из анти-паттернов и из text_rules.yaml."""
    stems = list(RULES.get("forbidden_in_solutions", []))
    for a in MATRIX["antipatterns"].values():
        stems += a.get("stems", [])
    return sorted(set(stems))


def card(move_id: str, *, world_notes=False) -> dict:
    """Заготовка решения; pain и pain_refs модель заполняет только фактами объекта."""
    m = MATRIX["moves"][move_id]
    out = {
        "move": m["id"],
        "name": m["name"],
        "service": PRODUCTS["products"][m["product"]],
        "pain": "",
        "pain_refs": [],
        "solution": m["solution"],
        "agency": list(m["agency"]),
        "developer": list(m["developer"]),
        "variants": [dict(v) for v in m["variants"]],
        "outcome": m["outcome"],
        "metric": m["metric"],
        "limits": m.get("limits", ""),
    }
    if world_notes and m.get("world_note"):
        out["note"] = m["world_note"]
    return out


def metadata(scope="dolshiki"):
    """A small matching vocabulary, without world-case narratives or agency claims."""
    if scope not in {"dolshiki", "city"}:
        raise ValueError("Неизвестный режим матрицы")
    return {
        "problems": [
            {key: row[key] for key in ("id", "problem", "topic")}
            for row in MATRIX["problems"].values()
            if row.get("scope", "dolshiki") == scope
        ],
        "tactics": [
            {key: row[key] for key in ("id", "name", "ru")} for row in MATRIX["tactics"].values()
        ],
        "accelerators": [
            {key: row[key] for key in ("id", "name", "sign")}
            for row in MATRIX["accelerators"].values()
        ],
        "factors": [
            {key: row[key] for key in ("id", "name", "signs")} for row in MATRIX["factors"].values()
        ],
    }


def validate_move(stage, product, code, scope="dolshiki"):
    """Validate generated references; unknown model output never indexes the library."""
    move = MATRIX["moves"].get(code)
    if not move:
        return ["Хода нет в матрице"]
    errors = []
    if stage not in move["stages"]:
        errors.append("Ход не подходит к текущей стадии")
    if scope not in move["scope"]:
        errors.append("Ход не подходит к тематическому режиму")
    if product not in {move["product"], PRODUCTS["products"][move["product"]]}:
        errors.append("Продукт хода изменён")
    return errors


validatemove = validate_move


def suggest(
    stage: int,
    problems=(),
    tactics=(),
    accelerators=(),
    factors=(),
    scope="dolshiki",
    limit=4,
    pool=10,
    *,
    has_public_communities=False,
    confirmed_communication_gap=False,
    confirmed_conditions=(),
    allow_city=False,
) -> dict:
    """Кандидаты в решения.

    stage — текущая стадия 1–4; problems — id из problems.yaml; tactics — id из tactics.yaml;
    accelerators и factors — id из context.yaml. Возвращает:
      picked — от двух до limit ходов с объяснением, почему выбран каждый;
      reserve — следующие по весу ходы, из которых модель может заменить выбранный;
      avoid — анти-паттерны, которые нельзя предлагать в этом разборе;
      responses — строки таблицы «протест — ответ — последствия» для найденных тактик.
    """
    if stage not in STAGE_LEVERS:
        raise ValueError("Стадия матрицы должна быть от 1 до 4")
    if scope != "dolshiki" and not (scope == "city" and allow_city):
        raise ValueError("Городской режим матрицы не включён")
    if not 2 <= limit <= 4 or not 0 <= pool <= 20:
        raise ValueError("Некорректный размер подборки матрицы")
    M = MATRIX
    problems, tactics, accelerators, factors = (
        list(dict.fromkeys(values)) for values in (problems, tactics, accelerators, factors)
    )
    for kind, values in (
        ("problems", problems),
        ("tactics", tactics),
        ("accelerators", accelerators),
        ("factors", factors),
    ):
        if any(value not in M[kind] for value in values):
            raise ValueError("Неизвестная ссылка в сопоставлении матрицы: " + kind)
    score, why = defaultdict(float), defaultdict(list)
    avoid = defaultdict(int)

    def add(mid, pts, reason):
        score[mid] += pts
        why[mid].append(reason)

    for q in problems:
        p = M["problems"][q]
        if p.get("scope", "dolshiki") != scope or stage not in p["stages"]:
            continue
        for i, mid in enumerate(p["moves"]):
            add(mid, (3, 2, 1, 1)[min(i, 3)], f"проблема {q}: {p['problem']}")
        for x in p.get("avoid", []):
            avoid[x] += 1
        for t in p.get("tactics", []):  # тактики, которые обычно стоят за проблемой
            if t not in tactics:
                for mid in M["tactics"][t]["first"][:1]:
                    add(mid, 0.5, f"типичная тактика {t} за проблемой {q}")
    for t in tactics:
        tt = M["tactics"][t]
        for mid in tt["first"]:
            add(mid, 2, f"тактика {t}: {tt['name']} — первый ответ")
        for mid in tt["then"]:
            add(mid, 1, f"тактика {t}: {tt['name']} — усиление")
        for x in tt.get("avoid", []):
            avoid[x] += 1
    for a in accelerators:
        aa = M["accelerators"][a]
        for i, mid in enumerate(aa["moves"]):
            add(mid, 1.5 if i == 0 else 1, f"ускоритель {a}: {aa['name']}")
    for f in factors:
        for mid in M["factors"][f]["moves"]:
            add(mid, 1, f"фактор устойчивости: {M['factors'][f]['name']}")

    ranked, conditional = [], []
    for mid, pts in score.items():
        m = M["moves"][mid]
        if stage not in m["stages"] or scope not in m["scope"]:
            continue
        if (mid == "I01" and not has_public_communities) or (
            mid == "B05" and mid not in confirmed_conditions
        ):
            conditional.append({"move": mid, "condition": m.get("limits", "")})
            continue
        if m["lever"] in STAGE_LEVERS[stage]:
            pts += 0.5
        ranked.append((pts, mid))
    ranked.sort(key=lambda x: (-x[0], x[1]))

    picked, reserve, by_lever, by_product = [], [], defaultdict(int), defaultdict(int)
    lo, hi = PRODUCTS["limits"]["min"], min(limit, PRODUCTS["limits"]["max"])
    for pts, mid in ranked:
        m = M["moves"][mid]
        item = {
            "move": mid,
            "name": m["name"],
            "product": m["product"],
            "lever": m["lever"],
            "score": round(pts, 1),
            "why": why[mid],
        }
        if len(picked) < hi and by_lever[m["lever"]] < 2 and by_product[m["product"]] < 2:
            picked.append(item)
            by_lever[m["lever"]] += 1
            by_product[m["product"]] += 1
        elif len(reserve) < pool:
            reserve.append(item)
    # Missing matching is not evidence of a communication problem or need for a
    # paid diagnostic. Only a caller's grounded current communication gap permits
    # these two planning/position candidates; a chat's existence alone does not.
    if len(picked) < lo:
        fallback = []
        if confirmed_communication_gap is True:
            fallback.extend(("I04", "A06"))
        if has_public_communities and any((problems, tactics, accelerators, factors)):
            fallback.append("I01")
        for mid in fallback:
            if len(picked) < lo and mid not in {p["move"] for p in picked}:
                m = M["moves"][mid]
                if stage not in m["stages"] or scope not in m["scope"]:
                    continue
                if by_lever[m["lever"]] >= 2 or by_product[m["product"]] >= 2:
                    continue
                picked.append(
                    {
                        "move": mid,
                        "name": m["name"],
                        "product": m["product"],
                        "lever": m["lever"],
                        "score": 0,
                        "why": [
                            "подтверждённая проблема и доступное сообщество: ранний сигнал"
                            if mid == "I01"
                            else "подтверждённый коммуникационный пробел: программа урегулирования"
                        ],
                    }
                )
                by_lever[m["lever"]] += 1
                by_product[m["product"]] += 1
        reserve = [row for row in reserve if row["move"] not in {p["move"] for p in picked}]

    resp = sorted({M["tactics"][t]["response"] for t in tactics})
    return {
        "stage": stage,
        "scope": scope,
        "picked": picked,
        "reserve": reserve,
        "avoid": [
            {
                "id": x,
                "name": M["antipatterns"][x]["name"],
                "why": M["antipatterns"][x]["why"],
                "instead": M["antipatterns"][x]["instead"],
            }
            for x, _ in sorted(avoid.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "responses": [{"id": r, **M["responses"][r]} for r in resp],
        "conditional": sorted(conditional, key=lambda row: row["move"]),
        "outlook_rule": (
            "три и более фактора устойчивости: конфликт не затухнет без действий"
            if len(list(factors)) >= 3
            else ""
        ),
    }


def validate() -> list:
    """Проверка ссылок, лимитов карточек и правил текста. Возвращает список нарушений."""
    M, errs = MATRIX, []
    stems = antipattern_stems()
    used_moves, used_cases = set(), set()

    def ref(kind, ids, where):
        for i in ids:
            if i not in M[kind]:
                errs.append(f"{where}: нет {kind} «{i}»")

    for mid, m in M["moves"].items():
        w = f"ход {mid}"
        for k, lim in CARD_LIMITS.items():
            if k in m and len(m[k]) > lim:
                errs.append(f"{w}: {k} {len(m[k])} > {lim}")
        if not 3 <= len(m["agency"]) <= 6:
            errs.append(f"{w}: строк агентства {len(m['agency'])}, нужно 3–6")
        if not 1 <= len(m["developer"]) <= 3:
            errs.append(f"{w}: строк застройщика {len(m['developer'])}, нужно 1–3")
        if len(m["variants"]) not in (0, 3, 4, 5):
            errs.append(f"{w}: вариантов {len(m['variants'])}, нужно 3–5 или ни одного")
        for s in m["agency"]:
            if len(s) > 90:
                errs.append(f"{w}: строка агентства {len(s)} > 90 — «{s}»")
        for s in m["developer"]:
            if len(s) > 70:
                errs.append(f"{w}: строка застройщика {len(s)} > 70 — «{s}»")
        for v in m["variants"]:
            if len(v["name"]) > 18:
                errs.append(f"{w}: название варианта {len(v['name'])} > 18 — «{v['name']}»")
            if len(v["text"]) > 75:
                errs.append(f"{w}: текст варианта {len(v['text'])} > 75 — «{v['text']}»")
        if m["product"] not in PRODUCTS["products"]:
            errs.append(f"{w}: продукта «{m['product']}» нет в каталоге")
        if m["lever"] not in M["levers"]:
            errs.append(f"{w}: нет рычага «{m['lever']}»")
        if not set(m["stages"]) <= {1, 2, 3, 4} or not set(m["scope"]) <= {"dolshiki", "city"}:
            errs.append(f"{w}: стадии или режим заданы неверно")
        ref("cases", m.get("cases", []), w)
        used_cases.update(m.get("cases", []))
        blob = json.dumps({k: m[k] for k in CARD_FIELDS if k in m}, ensure_ascii=False).lower()
        for st in stems:
            if re.search(st, blob):
                errs.append(f"{w}: в тексте шаблон анти-паттерна «{st}»")
        for ph in RULES["stop_phrases"]:
            if ph in blob:
                errs.append(f"{w}: стоп-фраза «{ph}»")
        for iw in RULES["internal_words"]:
            if re.search(rf"(?<![а-яё]){re.escape(iw)}(?![а-яё])", blob):
                errs.append(f"{w}: внутреннее слово «{iw}»")
        if "dolshiki" in m["scope"] and m["scope"][0] == "dolshiki":
            for bad in RULES["replace_terms"]:
                if re.search(rf"(?<![а-яё]){re.escape(bad)}", blob):
                    errs.append(f"{w}: термин «{bad}» в ходе для дольщиков")

    for tid, t in M["tactics"].items():
        w = f"тактика {tid}"
        ref("moves", t["first"] + t["then"], w)
        ref("antipatterns", t.get("avoid", []), w)
        ref("cases", t.get("cases", []), w)
        used_moves.update(t["first"] + t["then"])
        used_cases.update(t.get("cases", []))
        if t["family"] not in M["families"] or t["response"] not in M["responses"]:
            errs.append(f"{w}: семейство или тип ответа заданы неверно")
    for qid, q in M["problems"].items():
        w = f"проблема {qid}"
        ref("moves", q["moves"], w)
        ref("tactics", q.get("tactics", []), w)
        ref("antipatterns", q.get("avoid", []), w)
        used_moves.update(q["moves"])
        sc = q.get("scope", "dolshiki")
        for mid in q["moves"]:
            m = M["moves"].get(mid)
            if m and sc not in m["scope"]:
                errs.append(f"{w}: ход {mid} не для режима {sc}")
            if m and not set(q["stages"]) & set(m["stages"]):
                errs.append(f"{w}: ход {mid} не подходит ни к одной стадии проблемы")
    for xid, x in M["antipatterns"].items():
        ref("moves", x["instead"], f"анти-паттерн {xid}")
        ref("cases", x.get("cases", []), f"анти-паттерн {xid}")
    for key in ("types", "accelerators", "factors"):
        for i, o in M[key].items():
            ref("moves", o["moves"], f"{key} {i}")
            ref("cases", o.get("cases", []), f"{key} {i}")
            used_moves.update(o["moves"])
            used_cases.update(o.get("cases", []))
    for aid, a in M["accelerators"].items():
        if len(a["tree_title"]) > 36:
            errs.append(f"ускоритель {aid}: заголовок {len(a['tree_title'])} > 36")
        if len(a["tree_text"]) > 70:
            errs.append(f"ускоритель {aid}: текст {len(a['tree_text'])} > 70")
    for cid, c in M["cases"].items():
        w = f"кейс {cid}"
        ref("tactics", c["tactics"], w)
        ref("antipatterns", c["anti"], w)
        ref("moves", c["moves"], w)
        ref("types", c["types"], w)
        if c["region"] not in M["regions"]:
            errs.append(f"{w}: нет региона")
    for mid in M["moves"]:
        if mid not in used_moves:
            errs.append(f"ход {mid} не привязан ни к одной проблеме, тактике или ускорителю")
    for cid in M["cases"]:
        if cid not in used_cases:
            errs.append(f"кейс {cid} не использован ни в одном ходе или тактике")
    return errs


def stats() -> dict:
    M = MATRIX
    return {
        "ходы": len(M["moves"]),
        "варианты": sum(len(m["variants"]) for m in M["moves"].values()),
        "тактики": len(M["tactics"]),
        "проблемы": len(M["problems"]),
        "анти-паттерны": len(M["antipatterns"]),
        "ускорители": len(M["accelerators"]),
        "факторы": len(M["factors"]),
        "типы конфликтов": len(M["types"]),
        "кейсы": len(M["cases"]),
        "связки проблема→ход": sum(len(q["moves"]) for q in M["problems"].values()),
        "связки тактика→ход": sum(len(t["first"]) + len(t["then"]) for t in M["tactics"].values()),
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate")
    sub.add_parser("stats")
    c = sub.add_parser("card")
    c.add_argument("move")
    s = sub.add_parser("suggest")
    s.add_argument("--stage", type=int, required=True)
    s.add_argument("--problems", nargs="*", default=[])
    s.add_argument("--tactics", nargs="*", default=[])
    s.add_argument("--accelerators", nargs="*", default=[])
    s.add_argument("--factors", nargs="*", default=[])
    s.add_argument("--scope", default="dolshiki")
    a = ap.parse_args()
    if a.cmd == "validate":
        errs = validate()
        print("матрица: OK" if not errs else f"матрица: {len(errs)} нарушений")
        for e in errs:
            print("  ·", e)
        sys.exit(1 if errs else 0)
    elif a.cmd == "stats":
        for k, v in stats().items():
            print(f"{k}: {v}")
    elif a.cmd == "card":
        print(json.dumps(card(a.move), ensure_ascii=False, indent=2))
    else:
        print(
            json.dumps(
                suggest(a.stage, a.problems, a.tactics, a.accelerators, a.factors, a.scope),
                ensure_ascii=False,
                indent=2,
            )
        )
