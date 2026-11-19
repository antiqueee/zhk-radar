"""Opt-in daily chat workflow and a durable, non-retrying alert outbox."""

import json
import threading
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from radar.web import chat_analysis, chat_workflow, stoplist
from radar.web import report_author as author
from radar.web.configuration import configuration
from radar.web.operations import send_digest
from radar.web.store import now


class AutomationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nightly_chats: bool = False
    analyze_chats: bool = False
    auto_briefs: bool = False
    telegram_alerts: bool = False
    hour: int = Field(default=3, ge=0, le=23)
    max_records: int = Field(default=10, ge=1, le=50)
    max_label_batches: int = Field(default=2, ge=1, le=5)


def initialize(store):
    with store.connect() as db:
        db.executescript("""CREATE TABLE IF NOT EXISTS automation_settings(
            id INTEGER PRIMARY KEY,payload TEXT);
            CREATE TABLE IF NOT EXISTS daily_steps(day TEXT,name TEXT,status TEXT,payload TEXT,
                created_at TEXT,PRIMARY KEY(day,name));
            CREATE TABLE IF NOT EXISTS notifications(id TEXT PRIMARY KEY,title TEXT,status TEXT,
                error TEXT,created_at TEXT);""")
        db.execute(
            "INSERT OR IGNORE INTO automation_settings VALUES(1,?)",
            (AutomationSettings().model_dump_json(),),
        )


def settings(store):
    initialize(store)
    with store.connect() as db:
        return AutomationSettings.model_validate_json(
            db.execute("SELECT payload FROM automation_settings WHERE id=1").fetchone()[0]
        )


def save(store, value):
    initialize(store)
    with store.connect() as db:
        db.execute(
            "UPDATE automation_settings SET payload=? WHERE id=1", (value.model_dump_json(),)
        )
    return value.model_dump()


def status(store):
    cfg = settings(store)
    with store.connect() as db:
        rows = [
            {**dict(r), "payload": json.loads(r["payload"]) if r["payload"] else None}
            for r in db.execute("SELECT * FROM daily_steps ORDER BY created_at DESC LIMIT 30")
        ]
        notices = [
            dict(r)
            for r in db.execute("SELECT * FROM notifications ORDER BY created_at DESC LIMIT 30")
        ]
    return {"settings": cfg.model_dump(), "history": rows, "notifications": notices}


def notice(store, ident, title):
    initialize(store)
    with store.connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO notifications VALUES(?,?,'local',NULL,?)",
            (ident, title[:1000], now()),
        )


class DailyWorker:
    def __init__(self, store, telegram, sender=send_digest, *, brand_sync=None):
        self.store, self.telegram, self.sender = store, telegram, sender
        self.brand_sync = brand_sync
        self.stop = threading.Event()
        self.thread = None
        initialize(store)

    def step(self, day, name, action):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM daily_steps WHERE day=? AND name=?", (day, name)
            ).fetchone()
            if old:
                return
            db.execute("INSERT INTO daily_steps VALUES(?,?,'running',NULL,?)", (day, name, now()))
        try:
            result = action()
            state = (
                "partial"
                if isinstance(result, dict)
                and (
                    result.get("partial") or result.get("more") or result.get("status") == "partial"
                )
                else "completed"
            )
        except ValueError as exc:
            result = {"error": str(exc)}
            state = "failed"
        except Exception:
            result = {"error": "Операция не завершена; проверьте подключение"}
            state = "failed"
        with self.store.connect() as db:
            db.execute(
                "UPDATE daily_steps SET status=?,payload=? WHERE day=? AND name=?",
                (state, json.dumps(result), day, name),
            )
        if state == "failed":
            notice(self.store, day + name, "Ночной этап не завершён: " + name)

    def tick(self, clock=None):
        clock = clock or datetime.now(UTC)
        if self.brand_sync:
            self.brand_sync.tick(clock)
        cfg = settings(self.store)
        local = clock.astimezone(ZoneInfo("Europe/Moscow"))
        day = local.date().isoformat()
        if cfg.nightly_chats and local.hour >= cfg.hour:
            linked = list(dict.fromkeys(r["record_id"] for r in self.telegram.links()))
            eligible = [
                ident
                for ident in linked
                if self.store.get(ident)["dataset"] != "demo"
                and not stoplist.blocked(self.store, self.store.get(ident))
            ]
            if len(eligible) > cfg.max_records:
                notice(
                    self.store,
                    "daily-cap:" + day,
                    "Ночной лимит: часть чатов не обработана. Увеличьте лимит объектов.",
                )
            for ident in eligible[: cfg.max_records]:
                if self.stop.is_set():
                    return
                for link in self.telegram.links(ident):
                    self.step(
                        day,
                        "read:" + ident + ":" + link["url"],
                        lambda link=link: self.telegram.sync(link["record_id"], link["url"]),
                    )
                if cfg.analyze_chats:
                    for batch in range(cfg.max_label_batches):
                        self.step(
                            day,
                            f"label:{ident}:{batch}",
                            lambda ident=ident: chat_analysis.analyze(
                                self.store, ident, run_id="daily:" + day
                            ),
                        )
                self.step(
                    day,
                    "dynamics:" + ident,
                    lambda ident=ident: chat_workflow.record_alerts(self.store, ident, clock),
                )
                if cfg.auto_briefs:
                    self.step(
                        day,
                        "author:" + ident,
                        lambda ident=ident: author.generate(self.store, ident, "daily:" + day),
                    )
        # Detect new failures even if nightly processing is off.
        with self.store.connect() as db:
            failed = (
                list(
                    db.execute(
                        "SELECT id FROM scan_jobs WHERE status='failed' "
                        "ORDER BY created_at DESC LIMIT 10"
                    )
                )
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='scan_jobs'").fetchone()
                else []
            )
            alerts = (
                list(db.execute("SELECT id,record_id FROM local_alerts WHERE seen=0"))
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='local_alerts'").fetchone()
                else []
            )
        for row in failed:
            notice(
                self.store,
                "scan:" + row["id"],
                "Поиск завершился с ошибкой. Подробности в истории запуска.",
            )
        for row in alerts:
            notice(
                self.store,
                "chat:" + row["id"],
                "Изменилась динамика обсуждений: " + self.store.get(row["record_id"])["name"],
            )
        if not cfg.telegram_alerts:
            return
        credentials = configuration(self.store.root)
        if not credentials.get("TG_BOT_TOKEN") or not credentials.get("TG_GROUP_ID"):
            return
        with self.store.connect() as db:
            rows = list(
                db.execute(
                    "SELECT * FROM notifications WHERE status='local' ORDER BY created_at LIMIT 5"
                )
            )
        for row in rows:
            with self.store.connect() as db:
                claim = db.execute(
                    "UPDATE notifications SET status='sending' WHERE id=? AND status='local'",
                    (row["id"],),
                ).rowcount
            if not claim:
                continue
            try:
                self.sender(credentials, "Мегаполис Интеледженс\n" + row["title"])
                state, error = "sent", None
            except Exception:
                state, error = (
                    "uncertain",
                    "Доставка не подтверждена. Проверьте группу; автоматического повтора нет.",
                )
            with self.store.connect() as db:
                db.execute(
                    "UPDATE notifications SET status=?,error=? WHERE id=?",
                    (state, error, row["id"]),
                )

    def start(self):
        if self.brand_sync:
            self.brand_sync.resume()
        with self.store.connect() as db:
            db.execute(
                "UPDATE notifications SET status='uncertain',error='Прерывание во время доставки' "
                "WHERE status='sending'"
            )
            db.execute(
                "UPDATE daily_steps SET status='interrupted',payload='{}' WHERE status='running'"
            )
        self.thread = threading.Thread(target=self.loop, daemon=True, name="radar-daily")
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(2)

    def loop(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                notice(
                    self.store,
                    "daily-error:" + now()[:10],
                    "Фоновая обработка остановилась на ошибке. Проверьте настройки и историю.",
                )
            self.stop.wait(60)
