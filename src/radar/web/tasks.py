"""Durable queue for long object workflows. Completed content uses adapter caches."""

import json
import threading
from contextvars import ContextVar
from uuid import uuid4

from radar.web.model_gateway import ModelCallError
from radar.web.store import now

_CURRENT_TASK = ContextVar("radar_object_task", default=None)
_CANCELLED = "Подготовка остановлена. Сохранённые этапы доступны для повтора."


class TaskCancelled(RuntimeError):
    """Stop the current object task at a safe checkpoint."""


def check_task_cancelled():
    if (current := _CURRENT_TASK.get()) is None:
        return
    store, ident = current
    with store.connect() as db:
        row = db.execute(
            "SELECT cancel_requested FROM object_tasks WHERE id=?", (ident,)
        ).fetchone()
    if row and row[0]:
        raise TaskCancelled(_CANCELLED)


def report_progress(store, task_id, completed, label, detail=""):
    check_task_cancelled()
    with store.connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS object_task_progress(
            task_id TEXT PRIMARY KEY, completed INTEGER, label TEXT,
            detail TEXT, updated_at TEXT)""")
        db.execute(
            """INSERT INTO object_task_progress VALUES(?,?,?,?,?)
            ON CONFLICT(task_id) DO UPDATE SET
            completed=MAX(object_task_progress.completed,excluded.completed),
            label=excluded.label,detail=excluded.detail,updated_at=excluded.updated_at""",
            (task_id, completed, label, detail, now()),
        )


class TaskRunner:
    def __init__(self, store, handlers):
        self.store, self.handlers = store, handlers
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.threads = []
        with store.connect() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS object_tasks(
                id TEXT PRIMARY KEY,kind TEXT,record_id TEXT,status TEXT,result TEXT,
                error TEXT,created_at TEXT,updated_at TEXT,
                cancel_requested INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS object_task_progress(
                    task_id TEXT PRIMARY KEY,completed INTEGER,label TEXT,
                    detail TEXT,updated_at TEXT);
                """)
            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute("PRAGMA table_info(object_tasks)")}
            if "cancel_requested" not in columns:
                db.execute(
                    "ALTER TABLE object_tasks ADD COLUMN cancel_requested "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            db.execute("DROP INDEX IF EXISTS one_object_task")
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS one_active_task_per_record "
                "ON object_tasks(record_id) WHERE status IN ('queued','running')"
            )

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
        with self.store.connect() as db:
            progress = db.execute(
                "SELECT * FROM object_task_progress WHERE task_id=?", (row["id"],)
            ).fetchone()
        progress = {**dict(progress), "total": 6} if progress else None
        result = json.loads(row["result"]) if row["result"] else None
        if row["status"] == "completed":
            from radar.web.report_status import internal_download_status

            outcome = internal_download_status((result or {}).get("payload") or {})
            progress = {
                "completed": 6,
                "total": 6,
                "label": outcome["label"],
                "detail": outcome["detail"],
                "updated_at": row["updated_at"],
            }
        return {
            **dict(row),
            "progress": progress,
            "result": result,
        }

    def latest_report(self, record_id):
        record = self.store.get(record_id)
        with self.store.connect() as db:
            row = db.execute(
                "SELECT id FROM object_tasks WHERE record_id=? AND kind='author' "
                "ORDER BY created_at DESC LIMIT 1",
                (record_id,),
            ).fetchone()
        task = self.get(row[0]) if row else None
        if task and task["status"] == "completed" and record["dataset"] != "demo":
            from radar.web.configuration import configuration

            enabled = configuration(self.store.root).get("RADAR_REPORT_KIT", "").lower()
            payload = (task.get("result") or {}).get("payload") or {}
            if (
                enabled in {"1", "true"}
                and payload.get("report_v2", {}).get("meta", {}).get("schema_version")
                not in {"5", "8"}
                and not payload.get("report_qualification")
            ):
                return None
        return task

    def latest_for_record(self, record_id):
        self.store.get(record_id)
        with self.store.connect() as db:
            row = db.execute(
                "SELECT id FROM object_tasks WHERE record_id=? "
                "ORDER BY created_at DESC LIMIT 1",
                (record_id,),
            ).fetchone()
        return self.get(row[0]) if row else None

    def submit(self, kind, record_id):
        self.store.get(record_id)
        if kind not in self.handlers:
            raise ValueError("Неизвестная операция")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id,kind FROM object_tasks WHERE record_id=? "
                "AND status IN ('queued','running')",
                (record_id,),
            ).fetchone()
            if row:
                if row["kind"] == kind:
                    ident = row["id"]
                else:
                    raise ValueError("Для этой карточки уже выполняется другая операция.")
            else:
                ident = uuid4().hex
                db.execute(
                    "INSERT INTO object_tasks(id,kind,record_id,status,created_at,updated_at) "
                    "VALUES(?,?,?,'queued',?,?)",
                    (ident, kind, record_id, now(), now()),
                )
        self.wake.set()
        return self.get(ident)

    def cancel(self, ident):
        task = self.get(ident)
        if task["kind"] != "author":
            raise ValueError("Эта операция не поддерживает остановку из карточки.")
        with self.store.connect() as db:
            db.execute(
                "UPDATE object_tasks SET cancel_requested=(status='running'), "
                "error=CASE WHEN status='queued' THEN ? ELSE error END, "
                "status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END,updated_at=? "
                "WHERE id=? AND status IN ('queued','running') AND cancel_requested=0",
                (_CANCELLED, now(), ident),
            )
        return self.get(ident)

    def start(self):
        with self.store.connect() as db:
            db.execute(
                "UPDATE object_tasks SET updated_at=?,cancel_requested=0,"
                "status=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'interrupted' END,"
                "error=CASE WHEN cancel_requested=1 THEN ? ELSE "
                "'Приложение остановилось. Повтор использует сохранённые шаги.' END "
                "WHERE status='running'",
                (now(), _CANCELLED),
            )
        for index in range(2):
            worker = threading.Thread(
                target=self.loop, daemon=True, name=f"radar-object-tasks-{index + 1}"
            )
            self.threads.append(worker)
            worker.start()

    def close(self):
        self.stop.set()
        self.wake.set()
        for worker in self.threads:
            worker.join(2)

    def loop(self):
        while not self.stop.is_set():
            with self.store.connect() as db:
                row = db.execute(
                    "UPDATE object_tasks SET status='running',updated_at=? WHERE id=("
                    "SELECT id FROM object_tasks WHERE status='queued' "
                    "ORDER BY created_at,id LIMIT 1) RETURNING id,kind,record_id",
                    (now(),),
                ).fetchone()
            if not row:
                self.wake.wait(1)
                self.wake.clear()
                continue
            result, error, status = None, None, "completed"
            token = _CURRENT_TASK.set((self.store, row["id"]))
            try:
                check_task_cancelled()
                result = self.handlers[row["kind"]](row["record_id"], row["id"])
            except TaskCancelled as exc:
                status, error = "cancelled", str(exc)
            except (ValueError, ModelCallError) as exc:
                status, error = "failed", str(exc)
            except Exception:
                status, error = (
                    "failed",
                    "Операция не завершена. Сохранённые данные доступны для повтора.",
                )
            finally:
                _CURRENT_TASK.reset(token)
            with self.store.connect() as db:
                db.execute(
                    "UPDATE object_tasks SET status=?,result=?,error=?,updated_at=?,"
                    "cancel_requested=0 WHERE id=?",
                    (
                        status,
                        json.dumps(result) if result is not None else None,
                        error,
                        now(),
                        row["id"],
                    ),
                )
