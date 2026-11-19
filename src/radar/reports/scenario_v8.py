"""Сборка страницы «Сценарии и риски» из config/stage_playbook.yaml.

Модель даёт только три вещи: триггеры текущей стадии, выбор рисков для следующих уровней
и вывод одной фразой. Всё остальное подставляется из конфига.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
PLAYBOOK = yaml.safe_load((ROOT / "config_v8" / "stage_playbook.yaml").read_text(encoding="utf-8"))


def stages_block() -> list:
    return [{"n": n, **PLAYBOOK["stages"][n]} for n in (1, 2, 3, 4)]


def build_scenario(
    stage: int, triggers: list, risk_titles: dict, takeaway: str, subtitle: str
) -> dict:
    """Three grounded triggers and three configured risk titles per future level."""
    tree = PLAYBOOK["trees"][stage]
    levels = []
    for i, lv in enumerate(tree["levels"], 1):
        st = PLAYBOOK["stages"].get(lv.get("stage"), {})
        pkg = PLAYBOOK["packages"][lv["package"]]
        if lv["risks"] == "triggers":
            risks = triggers
        else:
            pool = {r["title"]: r for r in PLAYBOOK["risk_pools"][lv["risks"]]}
            risks = [
                {**pool[t], "refs": []} for t in risk_titles.get(i, risk_titles.get(str(i), []))
            ]
        if len(risks) != 3:
            raise ValueError(f"уровень {i}: нужно ровно три риска")
        levels.append(
            {
                "n": lv["n"],
                "name": lv.get("name") or st["name"],
                "tag": lv["tag"],
                "desc": lv.get("desc") or st["desc"],
                "risks_label": lv["risks_label"],
                "risks": risks,
                "exit": {"label": lv["label"], **pkg},
            }
        )
    steps = [
        {"label": p["label"], "name": lvl["exit"]["title"], "level": p["level"]}
        for p, lvl in zip(tree["price"], levels, strict=True)
    ]
    return {
        "subtitle": subtitle,
        "passed": tree["passed"],
        "levels": levels,
        "price": {"steps": steps, "takeaway": takeaway, "note": PLAYBOOK["price_note"]},
    }
