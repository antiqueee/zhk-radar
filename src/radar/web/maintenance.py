"""Verified local backups and bounded retention, independent of search credentials."""

import json
import os
import sqlite3
import threading
from datetime import UTC, datetime, timedelta

from radar.web.store import now

LOCK = threading.Lock()


def initialize(store):
    with store.connect() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS maintenance_runs("
            "id INTEGER PRIMARY KEY,status TEXT,payload TEXT,created_at TEXT)"
        )


def history(store):
    initialize(store)
    with store.connect() as db:
        return [
            {**dict(r), "payload": json.loads(r["payload"])}
            for r in db.execute("SELECT * FROM maintenance_runs ORDER BY id DESC LIMIT 14")
        ]


def backup(store, automatic=False):
    initialize(store)
    with LOCK:
        folder = store.root / "data/backups"
        folder.mkdir(parents=True, exist_ok=True)
        folder.chmod(0o700)
        if automatic:
            latest = next((r for r in history(store) if r["status"] == "ok"), None)
            if latest and latest["created_at"][:10] == now()[:10]:
                return latest["payload"]
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        target = folder / ("radar-" + stamp + ".sqlite3")
        temporary = target.with_suffix(".pending")
        # Create private file before SQLite opens it.
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            with store.connect() as source, sqlite3.connect(temporary) as destination:
                source.backup(destination)
                if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Резервная копия не прошла проверку целостности")
            os.replace(temporary, target)
            result = {
                "file": target.name,
                "bytes": target.stat().st_size,
                "scope": "SQLite; ключи .env и config копируются отдельно",
            }
            with store.connect() as db:
                db.execute(
                    "INSERT INTO maintenance_runs(status,payload,created_at) VALUES('ok',?,?)",
                    (json.dumps(result), now()),
                )
            # Remove only verified app-created backups, retain at least 14 latest files.
            known = sorted(folder.glob("radar-*.sqlite3"), reverse=True)
            for old in known[14:]:
                if old.stat().st_mtime < (datetime.now(UTC) - timedelta(days=14)).timestamp():
                    old.unlink()
            return result
        finally:
            temporary.unlink(missing_ok=True)


class Maintenance:
    def __init__(self, store):
        self.store = store
        self.stop = threading.Event()
        self.thread = None
        self.last_error = None

    def start(self):
        self.thread = threading.Thread(target=self.loop, daemon=True, name="radar-maintenance")
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(2)

    def loop(self):
        while not self.stop.is_set():
            try:
                backup(self.store, automatic=True)
                cutoff = (datetime.now(UTC) - timedelta(days=180)).isoformat()
                with self.store.connect() as db:
                    db.execute("DELETE FROM access_audit WHERE created_at<?", (cutoff,))
                    db.execute("DELETE FROM auth_sessions WHERE expires_at<?", (now(),))
                self.last_error = None
            except Exception:
                self.last_error = (
                    "Резервное копирование не завершено. Проверьте место и права диска."
                )
            self.stop.wait(3600)
