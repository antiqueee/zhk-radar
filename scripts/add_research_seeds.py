#!/usr/bin/env python3
"""Prioritize up to six research URLs; dry run by default, no network/model calls."""

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from radar.web.sources import canonical_url


def _canonical(value):
    try:
        return canonical_url(value)
    except (ValueError, TypeError, AttributeError):
        return None


def _plan(db, record, seeds):
    row = db.execute("SELECT payload FROM records WHERE id=?", (record,)).fetchone()
    if row is None:
        raise ValueError("Карточка с указанным ID не найдена")
    payload = json.loads(row[0])
    previous = payload.get("urls", [])
    if not isinstance(previous, list) or any(not isinstance(url, str) for url in previous):
        raise ValueError("Список ссылок карточки имеет некорректный формат")
    # Move the first existing equivalent to the front, preserving its exact value.
    # All old entries, even pre-existing duplicates, survive exactly once each.
    moved, prefix, added = set(), [], []
    for seed in seeds:
        index = next((i for i, url in enumerate(previous) if _canonical(url) == seed), None)
        if index is None:
            prefix.append(seed)
            added.append(seed)
        else:
            prefix.append(previous[index])
            moved.add(index)
    result = [*prefix, *(url for i, url in enumerate(previous) if i not in moved)]
    changed = result != previous
    return {**payload, "urls": result}, {
        "record_id": record,
        "changed": changed,
        "applied": False,
        "added_urls": added,
        "prioritized_urls": prefix,
        "previous_count": len(previous),
        "result_count": len(result),
        "backup": None,
    }


def _backup(path):
    folder = path.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup = folder / f"before-research-seeds-{stamp}-{uuid4().hex[:8]}.sqlite3"
    backup.touch(mode=0o600, exist_ok=False)
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as source:
            with sqlite3.connect(backup) as destination:
                source.backup(destination)
                if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Резервная копия не прошла проверку целостности")
    except BaseException:
        backup.unlink(missing_ok=True)
        raise
    return backup


def add_research_seeds(workspace, record, urls, *, apply=False):
    if not 1 <= len(urls) <= 6:
        raise ValueError("Укажите от одной до шести ссылок --url")
    seeds = list(dict.fromkeys(canonical_url(url) for url in urls))
    path = Path(workspace).resolve() / "data/local/workspace.sqlite3"
    if not path.is_file():
        raise ValueError("Рабочая база не найдена")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        _, result = _plan(db, record, seeds)
    if not apply or not result["changed"]:
        return result
    backup = _backup(path)
    # Read again under the write reservation, so concurrent changes are preserved.
    db = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=15)
    try:
        db.execute("BEGIN IMMEDIATE")
        payload, result = _plan(db, record, seeds)
        result["backup"] = str(backup)
        if result["changed"]:
            timestamp = datetime.now(UTC).isoformat()
            db.execute(
                "UPDATE records SET payload=?,revision=revision+1,updated_at=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), timestamp, record),
            )
            db.execute(
                "INSERT INTO events(record_id,action,detail,created_at) VALUES(?,?,?,?)",
                (record, "research-seeds-added", "Обновлены приоритетные ссылки исследования: "
                 f"{len(seeds)}; новых: {len(result['added_urls'])}", timestamp),
            )
            result["applied"] = True
        db.commit()
        return result
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--record", required=True)
    parser.add_argument("--url", required=True, action="append", help="Repeat up to six times")
    parser.add_argument("--apply", action="store_true", help="Apply after a verified SQLite backup")
    args = parser.parse_args()
    try:
        result = add_research_seeds(args.workspace, args.record, args.url, apply=args.apply)
    except (ValueError, sqlite3.Error) as error:
        parser.exit(1, f"Ссылки не изменены: {error}\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
