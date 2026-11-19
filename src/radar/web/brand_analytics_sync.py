"""One incremental import for all records, independent of report requests."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from radar.web import brand_analytics
from radar.web.brand_analytics_transport import FeedSettings, TransportError, fetch_page
from radar.web.configuration import configuration, write_secrets

_PREFIX = "BRAND_ANALYTICS_"
_PROFILE_KEYS = {
    "METHOD", "TOKEN_HEADER", "TOKEN_SCHEME", "REQUEST_FIELDS", "STATIC_PARAMS",
    "MESSAGES_PATH", "NEXT_CURSOR_PATH", "FIELD_MAP", "DATE_FORMAT", "SOURCE_TIMEZONE",
    "PAGE_SIZE",
}
_JSON_FIELDS = {"REQUEST_FIELDS", "STATIC_PARAMS", "FIELD_MAP"}
_PAGES_PER_TOPIC = 4
_MOSCOW = ZoneInfo("Europe/Moscow")


class Connection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: str = Field(max_length=1500)
    topic_ids: str = Field(max_length=400)
    token: str = Field(default="", max_length=2000, repr=False)
    profile: str | None = Field(default=None, max_length=16000, repr=False)
    scheduled: bool = False


def _profile(raw):
    if not raw or not raw.strip():
        return {}
    values = json.loads(raw)
    if not isinstance(values, dict) or not values.keys() <= _PROFILE_KEYS:
        raise ValueError("Проверьте параметры профиля Brand Analytics")
    for name, value in values.items():
        if name in _JSON_FIELDS and not isinstance(value, dict):
            raise ValueError("Параметры полей Brand Analytics должны быть объектами JSON")
        if name not in _JSON_FIELDS and (not isinstance(value, str) or any(
            char in value for char in "\r\n\x00"
        )):
            raise ValueError("Проверьте значения профиля Brand Analytics")
    return {
        _PREFIX + name: json.dumps(value, ensure_ascii=False) if name in _JSON_FIELDS else value
        for name, value in values.items()
    }


def _feed_key(cfg):
    values = {k: v for k, v in cfg.items() if k.startswith(_PREFIX) and k != _PREFIX + "TOKEN"}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


class BrandAnalyticsSync:
    def __init__(self, store, fetch=None):
        self.store = store
        self.fetch = fetch or fetch_page
        brand_analytics.initialize(store)
        with store.connect() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS brand_analytics_control (
                id INTEGER PRIMARY KEY CHECK(id=1), scheduled INTEGER NOT NULL DEFAULT 0);
                INSERT OR IGNORE INTO brand_analytics_control(id) VALUES(1);
                CREATE TABLE IF NOT EXISTS brand_analytics_sync_runs (
                id INTEGER PRIMARY KEY, feed TEXT NOT NULL, day TEXT NOT NULL,
                state TEXT NOT NULL, requested_at TEXT NOT NULL, finished_at TEXT,
                result TEXT, UNIQUE(feed, day));
                CREATE TABLE IF NOT EXISTS brand_analytics_checkpoints (
                feed TEXT NOT NULL, topic_id TEXT NOT NULL, since TEXT NOT NULL,
                window_end TEXT, cursor TEXT, PRIMARY KEY(feed, topic_id));
                CREATE TABLE IF NOT EXISTS brand_analytics_sync_usage (
                run_id INTEGER NOT NULL, topic_id TEXT NOT NULL, attempts INTEGER NOT NULL,
                pages INTEGER NOT NULL DEFAULT 0, received INTEGER NOT NULL DEFAULT 0,
                outside_window INTEGER NOT NULL DEFAULT 0, completed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(run_id, topic_id));""")

    def _settings(self):
        cfg = configuration(self.store.root)
        try:
            feed = FeedSettings.from_mapping(cfg)
        except ValueError:
            feed = None
        return cfg, feed

    def status(self):
        cfg, feed = self._settings()
        with self.store.connect() as db:
            active = db.execute("SELECT scheduled FROM brand_analytics_control WHERE id=1")
            scheduled = bool(active.fetchone()[0])
            row = db.execute(
                "SELECT state,requested_at,finished_at,result FROM brand_analytics_sync_runs "
                "WHERE feed=? ORDER BY id DESC LIMIT 1", (_feed_key(cfg),),
            ).fetchone()
        last = dict(row) if row else None
        if last and last["result"]:
            last["result"] = json.loads(last["result"])
        return {
            "configured": feed is not None,
            "token_saved": bool(cfg.get(_PREFIX + "TOKEN")),
            "endpoint": feed.endpoint if feed else "",
            "topic_ids": ", ".join(feed.topic_ids) if feed else "",
            "profile_saved": any(cfg.get(_PREFIX + name) is not None for name in _PROFILE_KEYS),
            "scheduled": scheduled,
            "last_run": last,
            "inventory": brand_analytics.status(self.store),
        }

    def save(self, data):
        cfg = configuration(self.store.root)
        updates = {
            _PREFIX + "ENDPOINT": data.endpoint.strip(),
            _PREFIX + "TOPIC_IDS": data.topic_ids.strip(),
        }
        if data.token:
            updates[_PREFIX + "TOKEN"] = data.token.strip()
        if data.profile is not None:
            updates.update({_PREFIX + name: None for name in _PROFILE_KEYS})
            updates.update(_profile(data.profile))
        merged = {k: v for k, v in {**cfg, **updates}.items() if v is not None}
        if not updates[_PREFIX + "ENDPOINT"] and not updates[_PREFIX + "TOPIC_IDS"]:
            if data.scheduled or data.token:
                raise ValueError("Укажите адрес метода и от одной до двух тем Brand Analytics")
        else:
            FeedSettings.from_mapping(merged)
        write_secrets(self.store.root, updates)
        with self.store.connect() as db:
            db.execute(
                "UPDATE brand_analytics_control SET scheduled=? WHERE id=1", (data.scheduled,)
            )
        return self.status()

    def resume(self):
        with self.store.connect() as db:
            db.execute("UPDATE brand_analytics_sync_runs SET state='queued' WHERE state='running'")

    def _enqueue(self, key, clock, *, retry=False):
        day = clock.astimezone(_MOSCOW).date().isoformat()
        with self.store.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO brand_analytics_sync_runs(feed,day,state,requested_at) "
                "VALUES(?,?,'queued',?)", (key, day, clock.isoformat()),
            )
            if retry:
                db.execute(
                    "UPDATE brand_analytics_sync_runs SET state='queued',finished_at=NULL "
                    "WHERE feed=? AND day=? AND state IN ('failed','partial') "
                    "AND json_extract(result,'$.error') IS NOT NULL "
                    "AND COALESCE(json_extract(result,'$.retry_at'),'')<=? "
                    "AND EXISTS(SELECT 1 FROM brand_analytics_sync_usage u "
                    "WHERE u.run_id=brand_analytics_sync_runs.id AND u.completed=0 "
                    "AND u.attempts<?)",
                    (key, day, clock.isoformat(), _PAGES_PER_TOPIC),
                )

    def submit(self, clock=None):
        cfg, feed = self._settings()
        if feed is None:
            raise ValueError("Сначала сохраните подключение к ленте сообщений Brand Analytics")
        self._enqueue(_feed_key(cfg), clock or datetime.now(UTC), retry=True)
        return self.status()

    def tick(self, clock=None):
        clock = clock or datetime.now(UTC)
        cfg, feed = self._settings()
        if feed is None:
            return
        key = _feed_key(cfg)
        with self.store.connect() as db:
            scheduled = db.execute("SELECT scheduled FROM brand_analytics_control WHERE id=1")
            scheduled = bool(scheduled.fetchone()[0])
        if scheduled and clock.astimezone(_MOSCOW).hour >= 3:
            self._enqueue(key, clock)
        with self.store.connect() as db:
            row = db.execute(
                "UPDATE brand_analytics_sync_runs SET state='running' WHERE id=(SELECT id FROM "
                "brand_analytics_sync_runs WHERE feed=? AND state='queued' AND NOT EXISTS "
                "(SELECT 1 FROM brand_analytics_sync_runs WHERE state='running') "
                "ORDER BY id LIMIT 1) RETURNING id,requested_at", (key,),
            ).fetchone()
        if not row:
            return
        end = datetime.fromisoformat(row["requested_at"])
        state = "completed"
        error = None
        retry_at = None
        try:
            for topic_id in feed.topic_ids:
                if not self._pull_topic(feed, key, topic_id, end, row["id"]):
                    state = "partial"
        except TransportError as exc:
            error = str(exc)
            delay = exc.retry_after or (60 if exc.status_code == 429 else 0)
            if delay:
                retry_at = (clock + timedelta(seconds=delay)).isoformat()
        except Exception:
            error = "Не удалось завершить импорт Brand Analytics"
        with self.store.connect() as db:
            totals = dict(db.execute(
                "SELECT COALESCE(SUM(attempts),0) attempts, COALESCE(SUM(pages),0) pages, "
                "COALESCE(SUM(received),0) received, "
                "COALESCE(SUM(outside_window),0) outside_window "
                "FROM brand_analytics_sync_usage WHERE run_id=?", (row["id"],),
            ).fetchone())
            if error:
                state = "partial" if totals["pages"] else "failed"
                totals["error"] = error
                if retry_at:
                    totals["retry_at"] = retry_at
            db.execute(
                "UPDATE brand_analytics_sync_runs SET state=?,finished_at=?,result=? WHERE id=?",
                (state, datetime.now(UTC).isoformat(), json.dumps(totals), row["id"]),
            )

    def _pull_topic(self, feed, key, topic_id, end, run_id):
        with self.store.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO brand_analytics_sync_usage(run_id,topic_id,attempts) "
                "VALUES(?,?,0)", (run_id, topic_id),
            )
            usage = db.execute(
                "SELECT attempts,completed FROM brand_analytics_sync_usage "
                "WHERE run_id=? AND topic_id=?", (run_id, topic_id),
            ).fetchone()
            if usage["completed"]:
                return True
            db.execute(
                "INSERT OR IGNORE INTO brand_analytics_checkpoints(feed,topic_id,since) "
                "VALUES(?,?,?)",
                (key, topic_id, (end - timedelta(days=2)).isoformat()),
            )
            row = db.execute(
                "SELECT since,window_end,cursor FROM brand_analytics_checkpoints "
                "WHERE feed=? AND topic_id=?", (key, topic_id),
            ).fetchone()
        since = datetime.fromisoformat(row["since"])
        end = datetime.fromisoformat(row["window_end"]) if row["window_end"] else end
        cursor = row["cursor"]
        if since >= end:
            return True
        seen = {cursor}
        for _ in range(usage["attempts"], _PAGES_PER_TOPIC):
            with self.store.connect() as db:
                db.execute(
                    "UPDATE brand_analytics_sync_usage SET attempts=attempts+1 "
                    "WHERE run_id=? AND topic_id=?", (run_id, topic_id),
                )
            page = self.fetch(feed, topic_id=topic_id, since=since, until=end, cursor=cursor)
            messages = [msg for msg in page.messages if _within_window(msg, since, end)]
            cursor = page.next_cursor
            if cursor is not None and cursor in seen:
                raise TransportError("Brand Analytics повторил страницу выгрузки")
            brand_analytics.ingest_messages(self.store, messages)
            checkpoint_start = since if cursor else max(since, end - timedelta(hours=2))
            outside = len(page.messages) - len(messages)
            with self.store.connect() as db:
                db.execute(
                    "UPDATE brand_analytics_checkpoints SET since=?,window_end=?,cursor=? "
                    "WHERE feed=? AND topic_id=?",
                    (checkpoint_start.isoformat(),
                     end.isoformat() if cursor else None, cursor, key, topic_id),
                )
                db.execute(
                    "UPDATE brand_analytics_sync_usage SET pages=pages+1,received=received+?, "
                    "outside_window=outside_window+?,completed=? WHERE run_id=? AND topic_id=?",
                    (len(messages), outside, not cursor, run_id, topic_id),
                )
            if not cursor:
                return True
            seen.add(cursor)
        return False


def _within_window(message, since, end):
    if not message.get("published_at"):
        return True
    try:
        date = datetime.fromisoformat(message["published_at"])
    except (TypeError, ValueError):
        return False
    return date.tzinfo is not None and since <= date <= end
