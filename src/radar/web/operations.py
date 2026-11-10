"""Opt-in weekly scheduling, durable delivery and transactional paid-call caps."""

import json
import threading
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, Field

from radar.web.configuration import configuration
from radar.web.discovery import SearchRequest
from radar.web.store import now

MOSCOW = ZoneInfo("Europe/Moscow")


class OperationsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    weekday: int = Field(default=0, ge=0, le=6)
    hour: int = Field(default=9, ge=0, le=23)
    region: str = Field(default="capitals", pattern="^(capitals|moscow|petersburg|russia)$")
    delivery: bool = False
    sync_chats: bool = False
    per_run: int = Field(default=20, ge=1, le=100)
    daily: int = Field(default=60, ge=1, le=1000)
    monthly: int = Field(default=500, ge=1, le=10000)


def initialize(store):
    with store.connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS operation_settings(
            id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL, next_run TEXT);
        CREATE TABLE IF NOT EXISTS api_calls(
            id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, service TEXT NOT NULL,
            created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS weekly_runs(
            due TEXT PRIMARY KEY, job_id TEXT NOT NULL, status TEXT NOT NULL,
            payload TEXT, error TEXT, updated_at TEXT NOT NULL);
        """)
        db.execute(
            "INSERT OR IGNORE INTO operation_settings VALUES(1,?,NULL)",
            (OperationsSettings().model_dump_json(),),
        )


def settings(store):
    initialize(store)
    with store.connect() as db:
        row = db.execute("SELECT * FROM operation_settings WHERE id=1").fetchone()
    return {**json.loads(row["payload"]), "next_run": row["next_run"], "timezone": "Europe/Moscow"}


def next_run(cfg, after):
    local = after.astimezone(MOSCOW)
    due = local.replace(hour=cfg.hour, minute=0, second=0, microsecond=0)
    due += timedelta(days=(cfg.weekday - due.weekday()) % 7)
    if due <= local:
        due += timedelta(days=7)
    return due.astimezone(UTC).isoformat()


def save(store, cfg):
    initialize(store)
    current = settings(store)
    timing_changed = any(current[k] != getattr(cfg, k) for k in ("enabled", "weekday", "hour"))
    due = (
        next_run(cfg, datetime.now(UTC))
        if timing_changed or not current["next_run"]
        else current["next_run"]
    )
    with store.connect() as db:
        db.execute(
            "UPDATE operation_settings SET payload=?,next_run=? WHERE id=1",
            (cfg.model_dump_json(), due if cfg.enabled else None),
        )
    return settings(store)


class BudgetExceeded(ValueError):
    pass


def reserve_call(store, run_id, service):
    """Reserve before external I/O; failed/uncertain calls still consume a slot."""
    initialize(store)
    stamp = now()
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        cfg = json.loads(
            db.execute("SELECT payload FROM operation_settings WHERE id=1").fetchone()[0]
        )
        counters = {
            "per_run": db.execute(
                "SELECT count(*) FROM api_calls WHERE run_id=?", (run_id,)
            ).fetchone()[0],
            "daily": db.execute(
                "SELECT count(*) FROM api_calls WHERE created_at>=?", (stamp[:10],)
            ).fetchone()[0],
            "monthly": db.execute(
                "SELECT count(*) FROM api_calls WHERE created_at>=?", (stamp[:7],)
            ).fetchone()[0],
        }
        for period, count in counters.items():
            if count >= cfg[period]:
                raise BudgetExceeded(
                    "Достигнут лимит платных запросов: "
                    + {"per_run": "на запуск", "daily": "за сутки UTC", "monthly": "за месяц UTC"}[
                        period
                    ]
                )
        db.execute(
            "INSERT INTO api_calls(run_id,service,created_at) VALUES(?,?,?)",
            (run_id, service, stamp),
        )


def usage(store):
    initialize(store)
    with store.connect() as db:
        counts = [
            dict(r)
            for r in db.execute(
                (
                    "SELECT service,count(*) AS calls FROM api_calls WHERE "
                    "created_at>=? GROUP BY service"
                ),
                (now()[:7],),
            )
        ]
    return {
        "month": now()[:7],
        "services": counts,
        "calls": sum(r["calls"] for r in counts),
        "cost_usd": None,
        "note": (
            "Счётчик попыток платных вызовов. Стоимость уточняйте у "
            "провайдера; кэш не расходует лимит."
        ),
    }


def send_digest(cfg, text, client=None):
    if not cfg.get("TG_BOT_TOKEN") or not cfg.get("TG_GROUP_ID"):
        raise ValueError("Для доставки сохраните токен бота и ID группы")
    owns = client is None
    client = client or httpx.Client(timeout=25)
    try:
        response = client.post(
            "https://api.telegram.org/bot" + cfg["TG_BOT_TOKEN"] + "/sendMessage",
            json={
                "chat_id": cfg["TG_GROUP_ID"],
                "text": text,
                "link_preview_options": {"is_disabled": True},
            },
        )
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise ValueError("Telegram не подтвердил доставку")
        return result["result"]["message_id"]
    except (httpx.HTTPError, KeyError, json.JSONDecodeError):
        # Never expose URLs (bot token) or blindly retry a possibly delivered message.
        raise ValueError(
            "Доставка не подтверждена. Проверьте группу перед повторной отправкой"
        ) from None
    finally:
        if owns:
            client.close()


def digest(store, job):
    result = job.get("result") or {}
    text = [
        "Мегаполис Интеледженс · недельный мониторинг",
        "Статус: " + job["status"],
        f"Найдено объектов: {result.get('found', 0)}. "
        f"Новых сигналов: {result.get('signals_added', 0)}.",
    ]
    for ident in result.get("record_ids", [])[:8]:
        record = store.get(ident)
        text.append(record["name"] + " · " + record["region"] + "\n" + record["summary"][:180])
    text.append("Выводы требуют проверки. Подробности и PDF доступны в локальном приложении.")
    return "\n\n".join(text)[:4000]


class WeeklyScheduler:
    def __init__(self, store, jobs, telegram, sender=send_digest):
        self.store, self.jobs, self.telegram, self.sender = store, jobs, telegram, sender
        self.stop = threading.Event()
        self.thread = None
        self.last_error = None
        initialize(store)

    def history(self):
        with self.store.connect() as db:
            return [
                dict(r) for r in db.execute("SELECT * FROM weekly_runs ORDER BY due DESC LIMIT 20")
            ]

    def start(self):
        with self.store.connect() as db:
            db.execute(
                "UPDATE weekly_runs SET status='uncertain',error='Приложение "
                "остановилось во время отправки. Проверьте группу.' WHERE "
                "status='sending'"
            )
            db.execute("UPDATE weekly_runs SET status='ready' WHERE status='preparing'")
        self.stop.clear()
        self.thread = threading.Thread(target=self.loop, daemon=True, name="radar-weekly")
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(2)

    def tick(self, clock=None):
        clock = clock or datetime.now(UTC)
        raw = settings(self.store)
        cfg = OperationsSettings.model_validate(
            {k: raw[k] for k in OperationsSettings.model_fields}
        )
        if cfg.enabled and raw["next_run"] and raw["next_run"] <= clock.isoformat():
            due = raw["next_run"]
            try:
                job = self.jobs.submit(
                    SearchRequest(region=cfg.region), idempotency_key="weekly:" + due
                )
            except ValueError as exc:
                self.last_error = str(exc)
                # Missing credentials/busy worker: retained due date, no external retries.
                return
            with self.store.connect() as db:
                db.execute(
                    "INSERT OR IGNORE INTO weekly_runs VALUES(?,?,'waiting',?,NULL,?)",
                    (due, job["id"], cfg.model_dump_json(), now()),
                )
                db.execute(
                    "UPDATE operation_settings SET next_run=? WHERE id=1", (next_run(cfg, clock),)
                )
        for row in self.history():
            if row["status"] != "waiting":
                continue
            job = self.jobs.get(row["job_id"])
            if job["status"] in {"queued", "running"}:
                continue
            snapshot = json.loads(row["payload"])
            chat_results = []
            if snapshot["sync_chats"] and job["status"] in {"completed", "partial"}:
                for link in self.telegram.links():
                    if self.stop.is_set():
                        return
                    if self.store.get(link["record_id"])["dataset"] == "demo":
                        continue
                    try:
                        chat_results.append(self.telegram.sync(link["record_id"], link["url"]))
                    except ValueError as exc:
                        chat_results.append({"error": str(exc)})
            message = digest(self.store, job)
            if chat_results:
                message += f"\n\nОбработано чатов: {len(chat_results)}."
                message += f" Ошибки: {sum('error' in r for r in chat_results)}."
            with self.store.connect() as db:
                db.execute(
                    "UPDATE weekly_runs SET status='ready',payload=?,updated_at=? WHERE due=?",
                    (
                        json.dumps(
                            {
                                "text": message,
                                "delivery": snapshot["delivery"],
                                "chats": chat_results,
                            }
                        ),
                        now(),
                        row["due"],
                    ),
                )
        for row in self.history():
            if row["status"] != "ready":
                continue
            payload = json.loads(row["payload"])
            # Disabling delivery takes effect even for a queued digest.
            if not payload["delivery"] or not settings(self.store)["delivery"]:
                self.finish(row["due"], "local", None)
                continue
            cfg = configuration(self.store.root)
            if not cfg.get("TG_BOT_TOKEN") or not cfg.get("TG_GROUP_ID"):
                self.finish(row["due"], "delivery_blocked", "Добавьте токен бота и ID группы")
                continue
            with self.store.connect() as db:
                changed = db.execute(
                    "UPDATE weekly_runs SET status='sending' WHERE due=? AND status='ready'",
                    (row["due"],),
                ).rowcount
            if not changed:
                continue
            try:
                self.sender(cfg, payload["text"])
                self.finish(row["due"], "sent", None)
            except ValueError as exc:
                self.finish(row["due"], "uncertain", str(exc))

    def finish(self, due, status, error):
        with self.store.connect() as db:
            db.execute(
                "UPDATE weekly_runs SET status=?,error=?,updated_at=? WHERE due=?",
                (status, error, now(), due),
            )

    def loop(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                # Keep scheduler alive; no credential-bearing exceptions in logs.
                self.last_error = (
                    "Ошибка фонового расписания. Проверьте историю и перезапустите приложение."
                )
            self.stop.wait(20)
