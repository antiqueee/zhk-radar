"""Automatic bounded public-chat screening as part of an internet scan."""

import json
import re

from radar.web import chat_analysis, chat_workflow, stoplist
from radar.web.discovery import ScanCancelled
from radar.web.dossiers import normalized, source_region
from radar.web.operations import settings
from radar.web.store import now
from radar.web.telegram import TelegramLink


def initialize(store):
    with store.connect() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS chat_screenings("
            "run_id TEXT,record_id TEXT,payload TEXT,created_at TEXT,"
            "PRIMARY KEY(run_id,record_id))"
        )


def matches(record, metadata):
    if not metadata.get("public") or not metadata.get("group"):
        return False, "Источник не является открытым чатом"
    text = normalized(metadata.get("title", "") + " " + metadata.get("description", ""))
    name = re.sub(r"^(жк|жилой комплекс)\s+", "", normalized(record["name"]))
    if len(name) < 4 or (" " + name + " ") not in (" " + text + " "):
        return False, "Название ЖК не подтверждено описанием чата"
    region = record["region"]
    if (
        region in {"Россия", "Не установлен", "Не указан", ""}
        or source_region(region, text) == "Не установлен"
    ):
        return False, "Регион не подтверждён описанием чата"
    building = normalized(record.get("building") or "")

    def canonical(value):
        return re.sub(r"\bкорп\s+", "корпус ", value)

    if building and (" " + canonical(building) + " ") not in (" " + canonical(text) + " "):
        return False, "Чат всего ЖК или другого корпуса: нужна проверка соответствия"
    return True, "Название, регион и указанный корпус совпали с описанием открытого чата"


def latest(store, ident):
    initialize(store)
    with store.connect() as db:
        row = db.execute(
            "SELECT payload FROM chat_screenings WHERE record_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (ident,),
        ).fetchone()
    return json.loads(row[0]) if row else None


def screen(
    store,
    ids,
    run_id,
    telegram,
    *,
    finder=None,
    analyzer=None,
    cancelled=lambda: False,
    progress=lambda *a: None,
):
    initialize(store)
    cfg = settings(store)
    if not cfg["screen_chats"]:
        return {
            "status": "disabled",
            "records": [],
            "note": "Скрининг чатов выключен в подключениях",
        }
    if telegram is None or not telegram.status().get("session_saved"):
        return {
            "status": "not_connected",
            "records": [],
            "note": "Telegram не подключён: выполнен только интернет-поиск",
        }
    finder = finder or chat_workflow.discover_chats
    analyzer = analyzer or chat_analysis.analyze
    results = []

    def check():
        if cancelled():
            raise ScanCancelled()

    for index, ident in enumerate(list(dict.fromkeys(ids))[:5]):
        check()
        record = store.get(ident)
        if (
            record["dataset"] == "demo"
            or stoplist.blocked(store, record)
            or record["status"] in {"rejected", "closed", "later"}
        ):
            continue
        with store.connect() as db:
            old = db.execute(
                "SELECT payload FROM chat_screenings WHERE run_id=? AND record_id=?",
                (run_id, ident),
            ).fetchone()
        if old:
            results.append(json.loads(old[0]))
            continue
        progress("chats", 80 + index, "Проверяем открытые чаты: " + record["name"])
        value = {
            "record_id": ident,
            "name": record["name"],
            "period_days": 30,
            "chats": [],
            "labelled": 0,
            "skipped": [],
            "errors": [],
            "status": "completed",
        }
        links = telegram.links(ident)
        # Existing links were selected earlier; new links require matching metadata.
        if not links:
            try:
                found = chat_workflow.candidates(store, ident)
                if not found:
                    found = finder(store, ident, run_id)["candidates"]
                for candidate in [c for c in found if c["status"] != "rejected"][:3]:
                    check()
                    url = candidate["url"]
                    try:
                        metadata = telegram.inspect(url)
                        accepted, reason = matches(record, metadata)
                        if not accepted:
                            value["skipped"].append({"url": url, "reason": reason})
                            continue
                        telegram.add(ident, TelegramLink(url=url))
                        with store.connect() as db:
                            payload = {
                                **candidate["payload"],
                                "auto_matched": True,
                                "match_reason": reason,
                                "matched_at": now(),
                            }
                            db.execute(
                                "UPDATE chat_candidates SET status='accepted',payload=? "
                                "WHERE record_id=? AND url=?",
                                (json.dumps(payload), ident, url),
                            )
                            # Pin identity against username reassignment.
                            db.execute(
                                "UPDATE telegram_links SET peer_id=? WHERE record_id=? AND url=?",
                                (metadata["peer_id"], ident, url),
                            )
                        links = telegram.links(ident)
                        break
                    except ValueError as exc:
                        value["skipped"].append({"url": url, "reason": str(exc)})
            except ValueError as exc:
                value["errors"].append(str(exc))
        # One group and at most 1500 recent messages per object, even if many groups are attached.
        for link in links[:1]:
            check()
            try:
                read = telegram.sync(ident, link["url"], limit=1500, recent=True)
                value["chats"].append(
                    {
                        "url": link["url"],
                        "read": read["read"],
                        "added": read["ingest"]["added"],
                        "partial": read["partial"],
                    }
                )
            except ValueError as exc:
                value["errors"].append(str(exc))
        if len(links) > 1:
            value["skipped"].append({"url": "", "reason": "Автоскрининг читает один чат на объект"})
        if value["chats"]:
            check()
            with store.connect() as db:
                calls = db.execute(
                    "SELECT count(*) FROM api_calls WHERE run_id=? AND service='chat-model'",
                    (run_id,),
                ).fetchone()[0]
            if cfg["screen_model_calls"] == 0:
                value["analysis_disabled"] = True
            elif calls < cfg["screen_model_calls"]:
                try:
                    result = analyzer(
                        store,
                        ident,
                        run_id=run_id,
                        max_messages=60,
                        text_budget=12000,
                        newest_first=True,
                    )
                    value["labelled"] = result["labelled"]
                    value["more_unlabelled"] = result["more"]
                except ValueError as exc:
                    value["errors"].append(str(exc))
            else:
                value["more_unlabelled"] = True
                value["errors"].append(
                    "Лимит модельных вызовов скрининга: тексты сохранены без новой разметки"
                )
        value["status"] = (
            "partial" if value["errors"] or value.get("more_unlabelled") else "completed"
        )
        if any(c["partial"] for c in value["chats"]) or value["skipped"]:
            value["status"] = "partial"
        value["note"] = (
            "Выборка: до 1500 последних сообщений за 30 дней из одного чата; "
            "разметка — до 60 сообщений и 12 000 знаков. "
            "Это не полный охват обсуждений. Спорные чаты не подключаются автоматически."
        )
        with store.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO chat_screenings VALUES(?,?,?,?)",
                (run_id, ident, json.dumps(value), now()),
            )
        results.append(value)
    return {
        "status": "partial" if any(r["status"] == "partial" for r in results) else "completed",
        "records": results,
        "note": "Открытые чаты: последние 30 дней, ограниченная выборка",
    }
