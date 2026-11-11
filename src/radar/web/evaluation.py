"""Offline quality measurement on a named, human-reviewed bounded reference set."""

import json
import math
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from radar.web.store import now


class Run(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    found_ids: list[str] = Field(max_length=10000)
    cost_usd: float = Field(default=0, ge=0, le=10000, allow_inf_nan=False)


class ChatLabel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    expected: str = Field(min_length=1, max_length=80)
    predicted: str = Field(min_length=1, max_length=80)


class ReportRating(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record_id: str = Field(min_length=1, max_length=100)
    accepted: bool
    factual_corrections: int = Field(ge=0, le=100)
    adaptation_score: int = Field(ge=1, le=5)


class Evaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=3, max_length=200)
    scope: str = Field(min_length=20, max_length=2000)
    reviewed_by: str = Field(default="", max_length=100)
    reviewed: bool = False
    synthetic: bool = True
    expected_ids: list[str] = Field(min_length=1, max_length=10000)
    runs: list[Run] = Field(min_length=1, max_length=10)
    chat_labels: list[ChatLabel] = Field(default_factory=list, max_length=10000)
    reports: list[ReportRating] = Field(default_factory=list, max_length=100)

    @field_validator("expected_ids")
    @classmethod
    def unique_ids(cls, rows):
        if len(rows) != len(set(rows)) or any(not i.strip() or len(i) > 200 for i in rows):
            raise ValueError("Эталонные ID должны быть непустыми и уникальными")
        return rows

    @model_validator(mode="after")
    def distinct(self):
        for rows, field in [
            (self.runs, "name"),
            (self.chat_labels, "id"),
            (self.reports, "record_id"),
        ]:
            if len(rows) != len({getattr(r, field) for r in rows}):
                raise ValueError("Повторная оценка одного элемента исказит результат")
        if self.reviewed and not self.reviewed_by.strip():
            raise ValueError("Укажите, кто сверил эталон и границы выборки")
        return self


def wilson(success, count):
    if not count:
        return None
    p, z = success / count, 1.96
    center = (p + z * z / (2 * count)) / (1 + z * z / count)
    spread = z * math.sqrt(p * (1 - p) / count + z * z / (4 * count * count)) / (1 + z * z / count)
    return [round(center - spread, 4), round(center + spread, 4)]


def measure(value):
    expected = set(value.expected_ids)
    rows = []
    usable = value.reviewed and not value.synthetic and len(expected) >= 20
    for run in value.runs:
        found = set(run.found_ids)
        tp, fp, fn = len(found & expected), len(found - expected), len(expected - found)
        precision = tp / len(found) if found else None
        recall = tp / len(expected)
        rows.append(
            {
                "name": run.name,
                "true_positive": tp,
                "false_positive": fp,
                "false_negative": fn,
                "precision": precision,
                "recall": recall,
                "f1": 2 * tp / (2 * tp + fp + fn),
                "precision_interval_95": wilson(tp, len(found)),
                "duplicates": len(run.found_ids) - len(found),
                "cost_usd": run.cost_usd,
                "missed_ids": sorted(expected - found),
                "extra_ids": sorted(found - expected),
                "passes_sample_threshold": bool(
                    usable and precision is not None and precision >= 0.9
                ),
            }
        )
    eligible = [r for r in rows if r["passes_sample_threshold"]]
    best = (
        max(eligible, key=lambda r: (r["recall"], r["precision"], -r["cost_usd"]))
        if eligible
        else None
    )
    correct = sum(c.expected == c.predicted for c in value.chat_labels)
    confusion = {}
    for c in value.chat_labels:
        key = c.expected + " → " + c.predicted
        confusion[key] = confusion.get(key, 0) + 1
    accepted = [r for r in value.reports if r.accepted and r.factual_corrections <= 1]
    return {
        "title": value.title,
        "scope": value.scope,
        "synthetic": value.synthetic,
        "reviewed": value.reviewed,
        "reference_size": len(expected),
        "runs": rows,
        "candidate": best["name"] if best else None,
        "usable_reference": usable,
        "chats": {
            "count": len(value.chat_labels),
            "correct": correct,
            "accuracy": correct / len(value.chat_labels) if value.chat_labels else None,
            "confusion": confusion,
        },
        "reports": {
            "count": len(value.reports),
            "accepted_with_at_most_one_correction": len(accepted),
            "pilot_gate": bool(usable and len(accepted) >= 5),
        },
        "note": "Полнота только относительно вручную сверенной выборки и указанного периода. "
        "ID обозначают уникальные события с корпусом и датой, а не URL перепечаток. "
        "Порог 90% относится к этой выборке; интервал показывает статистическую неопределённость. "
        "Синтетика, непроверенный эталон и выборка меньше 20 не выбирают победителя.",
    }


def initialize(store):
    with store.connect() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS quality_evaluations(id TEXT PRIMARY KEY,"
            "payload TEXT,result TEXT,created_at TEXT)"
        )


def save(store, value):
    initialize(store)
    result = measure(value)
    ident = uuid4().hex
    with store.connect() as db:
        db.execute(
            "INSERT INTO quality_evaluations VALUES(?,?,?,?)",
            (ident, value.model_dump_json(), json.dumps(result), now()),
        )
    return {"id": ident, **result}


def history(store):
    initialize(store)
    with store.connect() as db:
        return [
            {"id": r["id"], "created_at": r["created_at"], **json.loads(r["result"])}
            for r in db.execute(
                "SELECT * FROM quality_evaluations ORDER BY created_at DESC LIMIT 10"
            )
        ]


def example():
    return Evaluation(
        title="Учебный формат оценки",
        scope="Синтетические события двух корпусов за учебную неделю",
        expected_ids=["jk1:k1:delay:2026-11-10", "jk1:k2:defect:2026-11-10"],
        runs=[Run(name="Замените результатами поиска", found_ids=["jk1:k1:delay:2026-11-10"])],
    ).model_dump()
