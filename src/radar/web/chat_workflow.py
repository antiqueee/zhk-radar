"""Suggest public chat links and compare like-for-like weekly windows."""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta

import httpx

from radar.web import stoplist
from radar.web.chats import initialize as initialize_chats
from radar.web.configuration import configuration
from radar.web.discovery import yandex
from radar.web.dossiers import dossier
from radar.web.store import now
from radar.web.telegram import TelegramLink
from radar.web.workflow import cached, initialize


def candidates(store, ident):
    initialize(store)
    with store.connect() as db:
        return [
            {**dict(r), "payload": json.loads(r["payload"])}
            for r in db.execute(
                "SELECT * FROM chat_candidates WHERE record_id=? ORDER BY url", (ident,)
            )
        ]


def discover_chats(store, ident, run_id, client=None, searcher=yandex):
    initialize(store)
    record = store.get(ident)
    if record["dataset"] == "demo":
        return {
            "candidates": candidates(store, ident),
            "note": "Демо не вызывает внешний поиск чатов",
        }
    if stoplist.blocked(store, record):
        raise ValueError("Объект в стоп-листе")
    cfg = configuration(store.root)
    if not cfg.get("YANDEX_SEARCH_API_KEY") or not cfg.get("YANDEX_FOLDER_ID"):
        raise ValueError("Добавьте ключ и каталог Yandex Search")
    owns = client is None
    client = client or httpx.Client(timeout=60)
    try:
        query = f"{record['name']} {record['region']} чат жителей дольщиков site:t.me"
        rows, _ = cached(
            store,
            ["chat-search-v1", now()[:10], query],
            lambda: searcher(client, cfg, query, "month"),
            run_id,
            "search",
        )
        sources = [{"url": r["url"], "title": r["title"], "text": r["text"]} for r in rows]
        sources.extend(
            {"url": s["source_url"], "title": s["source_title"], "text": s["quote"]}
            for s in dossier(store, ident)["signals"]
        )
        found = {}
        for source in sources:
            for raw in re.findall(
                r"https://t\.me/(?:s/)?[A-Za-z][A-Za-z0-9_]{3,31}(?![\w])",
                source["url"] + " " + source["text"],
            ):
                try:
                    url = TelegramLink(url=raw).url
                except ValueError:
                    continue
                found.setdefault(
                    url,
                    {
                        "url": url,
                        "source_url": source["url"],
                        "title": source["title"],
                        "found_at": now(),
                        "verified": False,
                        "note": "Совпадение с ЖК и публичность нужно проверить перед подключением",
                    },
                )
        with store.connect() as db:
            for url, payload in list(found.items())[:10]:
                db.execute(
                    "INSERT OR IGNORE INTO chat_candidates VALUES(?,?,?,'suggested')",
                    (ident, url, json.dumps(payload)),
                )
        return {"candidates": candidates(store, ident), "searched": True}
    finally:
        if owns:
            client.close()


def decision(store, ident, url, accepted, telegram):
    link = TelegramLink(url=url)
    if not any(c["url"] == link.url for c in candidates(store, ident)):
        raise ValueError("Кандидат в чаты не найден")
    if accepted:
        telegram.add(ident, link)
    else:
        telegram.remove(ident, link)
    with store.connect() as db:
        db.execute(
            "UPDATE chat_candidates SET status=? WHERE record_id=? AND url=?",
            ("accepted" if accepted else "rejected", ident, link.url),
        )
    return candidates(store, ident)


def dynamics(store, ident, clock=None):
    initialize_chats(store)
    clock = clock or datetime.now(UTC)
    middle = clock - timedelta(days=7)
    start = clock - timedelta(days=14)
    with store.connect() as db:
        rows = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM local_messages WHERE record_id=? AND sent_at>=? AND sent_at<?",
                (ident, start.isoformat(), clock.isoformat()),
            )
        ]

    def window(messages):
        labelled = [m for m in messages if m["sentiment"] is not None]
        topics = {}
        for m in messages:
            if m["topic"] != "не_размечено":
                topics[m["topic"]] = topics.get(m["topic"], 0) + 1
        return {
            "messages": len(messages),
            "authors": len({m["author_hash"] for m in messages}),
            "labelled": len(labelled),
            "negative": sum(m["sentiment"] < 0 for m in labelled),
            "negative_share": round(
                sum(m["sentiment"] < 0 for m in labelled) * 100 / len(labelled), 1
            )
            if labelled
            else None,
            "topics": topics,
        }

    previous = window([r for r in rows if r["sent_at"] < middle.isoformat()])
    current = window([r for r in rows if r["sent_at"] >= middle.isoformat()])
    signals = []
    if previous["messages"] >= 5 and current["messages"] >= 10 and current["authors"] >= 5:
        if current["messages"] >= 2 * previous["messages"]:
            signals.append("Число сообщений выросло минимум вдвое при достаточной базе сравнения")
    if min(previous["labelled"], current["labelled"]) >= 10 and (
        current["negative_share"] - previous["negative_share"] >= 20
    ):
        signals.append("Доля негативных среди размеченных выросла минимум на 20 п.п.")
    return {
        "previous": previous,
        "current": current,
        "signals": signals,
        "start": start.isoformat(),
        "split": middle.isoformat(),
        "end": clock.isoformat(),
        "note": "Сравнение двух недель в подключённых чатах, не всех жителей. "
        "Рост активности не доказывает эскалацию; неполный сбор влияет на результат.",
    }


def record_alerts(store, ident, clock=None):
    value = dynamics(store, ident, clock)
    with store.connect() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS local_alerts(id TEXT PRIMARY KEY,record_id TEXT,"
            "kind TEXT,payload TEXT,created_at TEXT,seen INTEGER DEFAULT 0)"
        )
        for signal in value["signals"]:
            # At most once per type/record/Moscow calendar date; raw quotes never in alerts.
            from zoneinfo import ZoneInfo

            day = (
                (clock or datetime.now(UTC))
                .astimezone(ZoneInfo("Europe/Moscow"))
                .date()
                .isoformat()
            )
            key = hashlib.sha256((ident + signal + day).encode()).hexdigest()
            db.execute(
                "INSERT OR IGNORE INTO local_alerts VALUES(?,?,?,?,?,0)",
                (
                    key,
                    ident,
                    "chat_dynamics",
                    json.dumps({"signal": signal, "comparison": value}),
                    now(),
                ),
            )
    return value
