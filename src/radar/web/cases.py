"""Agency supplied cases; no invented results or implicit client publication consent."""

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from radar.web.store import now


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=5, max_length=180)
    situation: Literal["сроки", "приёмка", "после_заселения"]
    topics: list[str] = Field(min_length=1, max_length=15)
    description: str = Field(min_length=20, max_length=2000)
    result: str = Field(min_length=10, max_length=2000)
    evidence: str = Field(min_length=10, max_length=1000)
    approved: bool = False
    client_use: bool = False


def initialize(store):
    with store.connect() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS agency_cases("
            "id TEXT PRIMARY KEY,payload TEXT,updated_at TEXT)"
        )


def entries(store):
    initialize(store)
    with store.connect() as db:
        return [
            {"id": r["id"], **json.loads(r["payload"])}
            for r in db.execute("SELECT * FROM agency_cases")
        ]


def save(store, case):
    initialize(store)
    ident = hashlib.sha256(case.title.casefold().strip().encode()).hexdigest()[:24]
    with store.connect() as db:
        db.execute(
            "INSERT INTO agency_cases VALUES(?,?,?) ON CONFLICT(id) "
            "DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",
            (ident, case.model_dump_json(), now()),
        )
    return {"id": ident, **case.model_dump()}


def matching(store, payload):
    topics = set(payload["topics"])
    return sorted(
        [
            c
            for c in entries(store)
            if c["approved"]
            and c["situation"] == payload["situation"]
            and topics.intersection(c["topics"])
        ],
        key=lambda c: (-len(topics.intersection(c["topics"])), c["id"]),
    )[:2]
