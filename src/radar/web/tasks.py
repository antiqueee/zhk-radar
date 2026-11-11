"""Durable queue for long object workflows. Completed content uses adapter caches."""

import json
import threading
from uuid import uuid4

from radar.web.store import now


class TaskRunner:
    def __init__(self, store, handlers):
        self.store, self.handlers = store, handlers
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        with store.connect() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS object_tasks(
                id TEXT PRIMARY KEY,kind TEXT,record_id TEXT,status TEXT,result TEXT,
                error TEXT,created_at TEXT,updated_at TEXT);
                CREATE UNIQUE INDEX IF NOT EXISTS one_object_task ON object_tasks((1))
                WHERE status IN ('queued','running');""")

    def get(self, ident=None):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM object_tasks WHERE id=?"
                if ident
                else "SELECT * FROM object_tasks ORDER BY created_at DESC LIMIT 1",
                (ident,) if ident else (),
            ).fetchone()
        if not row:
            if ident:
                raise KeyError(ident)
            return None
        return {**dict(row), "result": json.loads(row["result"]) if row["result"] else None}

    def submit(self, kind, record_id):
        self.store.get(record_id)
        if kind not in self.handlers:
            raise ValueError("Неизвестная операция")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM object_tasks WHERE status IN ('queued','running')"
            ).fetchone()
            if row:
                if row["kind"] == kind and row["record_id"] == record_id:
                    ident = row["id"]
                else:
                    raise ValueError("Другая операция выполняется. Дождитесь завершения.")
            else:
                ident = uuid4().hex
                db.execute(
                    "INSERT INTO object_tasks VALUES(?,?,?,'queued',NULL,NULL,?,?)",
                    (ident, kind, record_id, now(), now()),
                )
        self.wake.set()
        return self.get(ident)

    def start(self):
        with self.store.connect() as db:
            db.execute(
                "UPDATE object_tasks SET status='interrupted',"
                "error='Приложение остановилось. Повтор использует сохранённые шаги.' "
                "WHERE status='running'"
            )
        self.thread = threading.Thread(target=self.loop, daemon=True, name="radar-object-tasks")
        self.thread.start()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join(2)

    def loop(self):
        while not self.stop.is_set():
            with self.store.connect() as db:
                row = db.execute(
                    "SELECT * FROM object_tasks WHERE status='queued' LIMIT 1"
                ).fetchone()
                if row:
                    db.execute(
                        "UPDATE object_tasks SET status='running',updated_at=? WHERE id=?",
                        (now(), row["id"]),
                    )
            if not row:
                self.wake.wait(1)
                self.wake.clear()
                continue
            result, error, status = None, None, "completed"
            try:
                result = self.handlers[row["kind"]](row["record_id"], row["id"])
            except ValueError as exc:
                status, error = "failed", str(exc)
            except Exception:
                status, error = (
                    "failed",
                    "Операция не завершена. Сохранённые данные доступны для повтора.",
                )
            with self.store.connect() as db:
                db.execute(
                    "UPDATE object_tasks SET status=?,result=?,error=?,updated_at=? WHERE id=?",
                    (
                        status,
                        json.dumps(result) if result is not None else None,
                        error,
                        now(),
                        row["id"],
                    ),
                )
