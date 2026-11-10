"""Offline import of labelled open-chat extracts. No Telegram session or sending."""

import hashlib
import hmac
import re
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=80)
    author_id: str = Field(min_length=1, max_length=80)
    author_name: str = Field(default="", max_length=160)
    sent_at: AwareDatetime
    text: str = Field(max_length=10000)
    topic: str = Field(default="не_размечено", max_length=80)
    sentiment: Literal[-2, -1, 0, 1, 2] | None = None


class ChatImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record_id: str
    chat_id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=180)
    access: Literal["public"]
    messages: list[Message] = Field(max_length=10000)


def redact(text: str, names: list[str]) -> str:
    for name in sorted({n for n in names if len(n) > 2}, key=len, reverse=True):
        text = re.sub(re.escape(name), "[имя]", text, flags=re.I)
    text = re.sub(r"\b[\w.+-]+@[\w.-]+\.[\w-]+\b", "[почта]", text)
    text = re.sub(r"(?<!\w)@[\w.]+", "[ник]", text)
    text = re.sub(
        r"(?<!\w)(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}\b", "[телефон]", text
    )
    return re.sub(
        r"\b(?:кв\.?|квартира|квартиры)\s*№?\s*\d+[а-яa-z]?", "[квартира]", text, flags=re.I
    )


def initialize(store):
    with store.connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS local_chats(
          record_id TEXT NOT NULL, chat_id TEXT NOT NULL, title TEXT NOT NULL,
          PRIMARY KEY(record_id,chat_id));
        CREATE TABLE IF NOT EXISTS local_messages(
          record_id TEXT NOT NULL, chat_id TEXT NOT NULL, message_id TEXT NOT NULL,
          author_hash TEXT NOT NULL, sent_at TEXT NOT NULL, text TEXT NOT NULL,
          topic TEXT NOT NULL, sentiment INTEGER,
          PRIMARY KEY(record_id,chat_id,message_id));
        """)


def ingest_chat(store, batch: ChatImport, salt: str):
    if not salt:
        raise ValueError("Не настроена локальная соль авторов")
    store.get(batch.record_id)
    initialize(store)
    count = 0
    names = [m.author_name for m in batch.messages]
    today = datetime.now(UTC)
    if any(m.sent_at > today + timedelta(minutes=5) for m in batch.messages):
        raise ValueError("В выгрузке есть сообщения из будущего")
    with store.connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO local_chats VALUES(?,?,?)",
            (batch.record_id, batch.chat_id, batch.title),
        )
        for message in batch.messages:
            if not message.text.strip():
                continue
            hashed = hmac.new(salt.encode(), message.author_id.encode(), hashlib.sha256).hexdigest()
            clean = (
                redact(message.text, names)
                if message.sent_at >= today - timedelta(days=180)
                else ""
            )
            count += db.execute(
                "INSERT OR IGNORE INTO local_messages VALUES(?,?,?,?,?,?,?,?)",
                (
                    batch.record_id,
                    batch.chat_id,
                    message.id,
                    hashed,
                    message.sent_at.astimezone(UTC).isoformat(),
                    clean,
                    message.topic,
                    message.sentiment,
                ),
            ).rowcount
    return {"added": count, "received": len(batch.messages), "record_id": batch.record_id}


def aggregates(store, record_id):
    initialize(store)
    cutoff = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    retention = (datetime.now(UTC) - timedelta(days=180)).isoformat()
    with store.connect() as db:
        db.execute("UPDATE local_messages SET text='' WHERE sent_at<? AND text!=''", (retention,))
        messages = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM local_messages WHERE record_id=? AND sent_at>=?", (record_id, cutoff)
            )
        ]
        chats = [
            dict(r)
            for r in db.execute(
                "SELECT chat_id,title FROM local_chats WHERE record_id=?", (record_id,)
            )
        ]
    topics = {}
    daily = {}
    for message in messages:
        topic = message["topic"]
        topics[topic] = topics.get(topic, 0) + 1
        day = message["sent_at"][:10]
        daily[day] = daily.get(day, 0) + 1
    labelled = [m for m in messages if m["sentiment"] is not None]
    return {
        "record_id": record_id,
        "chats": chats,
        "messages_30d": len(messages),
        "authors_30d": len({m["author_hash"] for m in messages}),
        "labelled": len(labelled),
        "negative_share": round(sum(m["sentiment"] < 0 for m in labelled) / len(labelled) * 100, 1)
        if labelled
        else None,
        "topics": sorted(topics.items(), key=lambda x: -x[1]),
        "daily": sorted(daily.items()),
        "method": (
            "Темы и тональность взяты из выгрузки; автоматическая разметка моделью не выполнялась"
        ),
    }
