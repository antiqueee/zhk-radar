#!/usr/bin/env python3
"""Prepare two scoped report-test cards; dry run by default, no network or model calls."""

import argparse
import json
import sqlite3
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

KROST_ID = "d2bdf37fff131d9ffb450ff5e5955e48d9d287a7b23f921489d9b1c4ce1f9b1f"
KROST_DUPLICATE_ID = "c28f66c3159e89f1c3dee55861507eaef1af04c6981ed282db0501c4c6ad93f9"
DOGMA_ID = "web-02803d5230c596b63102f25a"
TARGET_IDS = (KROST_ID, KROST_DUPLICATE_ID, DOGMA_ID)
DOGMA_SEEDS = (
    "https://dogma.ru/projects/dogma-park",
    "https://ap-r.ru/stats/zhk/zhk-dogma-park/",
    "https://zhk-dogma-park-krasnodar-i.cian.ru/otzyvy/",
)


def unique(values):
    """Stable exact deduplication preserves complete structured source values."""
    seen, result = set(), []
    for value in values:
        key = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            result.append(deepcopy(value))
    return result


def as_list(value):
    return value if isinstance(value, list) else ([] if value is None else [value])


def merge_claims(first, second):
    result = deepcopy(first)
    for key, value in second.items():
        if key not in result:
            result[key] = deepcopy(value)
        elif result[key] != value and isinstance(result[key], str) and isinstance(value, str):
            # Preserve both source texts without repeatedly appending the same imported claim.
            if value not in result[key]:
                result[key] += "\n" + value
        # Non-text conflicts stay intact in each original source_imports payload.
    return result


def source_import(row):
    original = json.loads(row["payload"])
    original.pop("source_imports", None)  # Existing provenance is merged separately, not nested.
    return {
        "record_id": row["id"],
        "dataset": row["dataset"],
        "status": row["status"],
        "note": row["note"],
        **{
            key: deepcopy(original[key])
            for key in ("file", "sheet", "row", "sha256", "cells")
            if key in original
        },
        "payload": original,
    }


def preserve_sources(payload, rows):
    imports = []
    for row in rows:
        imports.extend(as_list(json.loads(row["payload"]).get("source_imports")))
    for row in rows:
        if not any(
            isinstance(item, dict) and item.get("record_id") == row["id"] for item in imports
        ):
            imports.append(source_import(row))
    payload["source_imports"] = unique(imports)
    payload["source_record_ids"] = unique(
        [*as_list(payload.get("source_record_ids")), *(row["id"] for row in rows)]
    )


def prepare_payloads(rows):
    """Build changes without modifying the database or deleting any imported record."""
    canonical, duplicate, dogma = (rows[ident] for ident in TARGET_IDS)
    krost = json.loads(canonical["payload"])
    raw_duplicate = json.loads(duplicate["payload"])
    krost["claims"] = merge_claims(krost.get("claims", {}), raw_duplicate.get("claims", {}))
    for key in ("urls", "sources", "questions"):
        if key in krost or key in raw_duplicate:
            krost[key] = unique([*as_list(krost.get(key)), *as_list(raw_duplicate.get(key))])
    preserve_sources(krost, [canonical, duplicate])
    krost.update(
        {
            "name": "Новая Звезда — II очередь",
            "developer": "КРОСТ",
            "region": "Москва",
            "address": "Коммунарка, ул. Бачуринская",
            "aliases": ["Новая Звезда 2", "Новая Звезда II очередь", "Новая Звезда"],
            "official_domains": ["krost.ru"],
            "eisgs_id_hints": ["66399"],
            "report_scope": {
                "house_id": "66399",
                "phase": "II очередь",
                "exclude_locations": ["Расплетина 14", "I очередь сдана"],
            },
            "summary": "Тестовый подробный разбор: перепроверяем текущую проблематику, "
            "хронологию и позицию застройщика по II очереди на Бачуринской. "
            "Исторические утверждения из импорта требуют проверки актуальности.",
            "check": "Проверить ЕИСЖС 66399, II очередь, актуальные сроки и претензии; "
            "не смешивать с объектом на Расплетина, 14 и сданной I очередью.",
            "object_test_preparation": {"version": 1, "kind": "v8_full_report", "canonical": True},
        }
    )
    merged = deepcopy(raw_duplicate)
    merged["merged_into"] = KROST_ID
    dogma_payload = json.loads(dogma["payload"])
    preserve_sources(dogma_payload, [dogma])
    dogma_payload["urls"] = unique([*as_list(dogma_payload.get("urls")), *DOGMA_SEEDS])
    dogma_payload.update(
        {
            "name": "Догма Парк",
            "developer": "DOGMA",
            "region": "Краснодарский край, Краснодар",
            "address": "ул. Марины Цветаевой",
            "aliases": ["DOGMA PARK", "Догма Парк"],
            "official_domains": ["dogma.ru"],
            "summary": "Тестовый подробный разбор: перепроверяем текущую проблематику, "
            "хронологию и позицию застройщика. Положительное историческое "
            "утверждение из исходной карточки сохранено и требует проверки актуальности.",
            "check": "Проверить текущие сроки, корпуса и источники претензий в Догма Парк; "
            "не считать старое положительное утверждение подтверждением текущего состояния.",
            "object_test_preparation": {"version": 1, "kind": "v8_full_report", "canonical": True},
        }
    )
    return {
        KROST_ID: ("web", krost),
        KROST_DUPLICATE_ID: (duplicate["dataset"], merged),
        DOGMA_ID: ("web", dogma_payload),
    }


def prepare_workspace(workspace, *, apply=False):
    path = Path(workspace).resolve() / "data/local/workspace.sqlite3"
    if not path.is_file():
        raise ValueError("Рабочая база не найдена")
    uri = path.as_uri() + ("?mode=rw" if apply else "?mode=ro")
    db = sqlite3.connect(uri, uri=True, timeout=15)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        rows = {
            row["id"]: row
            for row in db.execute("SELECT * FROM records WHERE id IN (?,?,?)", TARGET_IDS)
        }
        if set(rows) != set(TARGET_IDS):
            raise ValueError("Не все три исходные карточки найдены; изменения не применены")
        prepared = prepare_payloads(rows)
        changed = []
        timestamp = datetime.now(UTC).isoformat()
        for ident, (dataset, payload) in prepared.items():
            before = rows[ident]
            if dataset == before["dataset"] and payload == json.loads(before["payload"]):
                continue
            changed.append(ident)
            if apply:
                db.execute(
                    "UPDATE records SET dataset=?,payload=?,revision=revision+1,updated_at=? "
                    "WHERE id=?",
                    (dataset, json.dumps(payload, ensure_ascii=False), timestamp, ident),
                )
                db.execute(
                    "INSERT INTO events(record_id,action,detail,created_at) VALUES(?,?,?,?)",
                    (ident, "object-test-prepared", "v8 scoped report test preparation", timestamp),
                )
        if apply:
            db.commit()
        else:
            db.rollback()
        return {
            "applied": apply,
            "changed": len(changed),
            "record_ids": changed,
            "canonical_id": KROST_ID,
            "merged_id": KROST_DUPLICATE_ID,
            "dogma_id": DOGMA_ID,
            "deleted_records": 0,
        }
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--apply", action="store_true", help="Apply atomically; default is dry run")
    args = parser.parse_args()
    try:
        result = prepare_workspace(args.workspace, apply=args.apply)
    except (ValueError, sqlite3.Error) as error:
        parser.exit(1, f"Подготовка не выполнена: {error}\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
