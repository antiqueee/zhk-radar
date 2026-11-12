"""Explicit relationship exclusions; approximate names never silently block an object."""

import re
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from radar.web.store import now


class StopEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["record", "developer"]
    value: str = Field(min_length=2, max_length=200)
    reason: Literal["client", "partner", "refused", "other"] = "other"
    note: str = Field(default="", max_length=1000)


def normalized(value):
    return re.sub(r"[^a-zа-я0-9]+", " ", value.lower().replace("ё", "е")).strip()


def initialize(store):
    with store.connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS stoplist(
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, value TEXT NOT NULL,
            normalized TEXT NOT NULL, reason TEXT NOT NULL, note TEXT NOT NULL,
            actor TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(kind,normalized))""")


def entries(store):
    initialize(store)
    with store.connect() as db:
        return [dict(r) for r in db.execute("SELECT * FROM stoplist ORDER BY created_at DESC")]


def add(store, entry, actor="local"):
    initialize(store)
    if entry.kind == "record":
        store.get(entry.value)
    elif not normalized(entry.value):
        raise ValueError("Укажите название застройщика")
    with store.connect() as db:
        db.execute(
            "INSERT INTO stoplist VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(kind,normalized) DO UPDATE SET reason=excluded.reason,note=excluded.note",
            (
                uuid4().hex,
                entry.kind,
                entry.value,
                normalized(entry.value),
                entry.reason,
                entry.note,
                actor,
                now(),
            ),
        )
    return entries(store)


def remove(store, ident):
    initialize(store)
    with store.connect() as db:
        db.execute("DELETE FROM stoplist WHERE id=?", (ident,))


def match(record, rows):
    # A per-object restore must never silently remove an exclusion for the whole developer.
    for row in sorted(rows, key=lambda r: r["kind"] != "developer"):
        value = record["id"] if row["kind"] == "record" else record["developer"]
        if row["normalized"] == normalized(value):
            return {
                "id": row["id"],
                "kind": row["kind"],
                "reason": row["reason"],
                "note": row["note"],
            }
    return None


def blocked(store, record):
    return match(record, entries(store))
