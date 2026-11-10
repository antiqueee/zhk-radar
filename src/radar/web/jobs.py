"""Single local worker with durable jobs. A page reload does not stop a scan."""

import fcntl
import json
import logging
import threading
from uuid import uuid4

from radar.web.discovery import ScanCancelled, SearchRequest, discover, ready
from radar.web.sources import initialize_sources
from radar.web.store import now

ACTIVE = ("queued", "running")


class JobRunner:
    def __init__(self, store):
        self.store = store
        initialize_sources(store)
        self.lease = None
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.lock = threading.Lock()
        with store.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS scan_jobs(
              id TEXT PRIMARY KEY, mode TEXT NOT NULL, request TEXT NOT NULL,
              status TEXT NOT NULL, stage TEXT NOT NULL, percent INTEGER NOT NULL DEFAULT 0,
              detail TEXT NOT NULL, result TEXT, error TEXT, cancel_requested INTEGER DEFAULT 0,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_scan ON scan_jobs((1))
              WHERE status IN ('queued','running');
            CREATE TABLE IF NOT EXISTS scan_job_events(
              id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, stage TEXT NOT NULL,
              percent INTEGER NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
            """)

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.lease = (self.store.path.parent / "worker.lock").open("a")
            try:
                fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.lease.close()
                self.lease = None
                raise RuntimeError("Другой экземпляр приложения уже запущен") from None
            with self.store.connect() as db:
                db.execute(
                    "UPDATE scan_jobs SET status='interrupted',stage='interrupted',"
                    "error='Приложение было остановлено. Можно повторить с сохранённых шагов.',"
                    "updated_at=? WHERE status='running'",
                    (now(),),
                )
            self.stop.clear()
            self.thread = threading.Thread(target=self.loop, name="radar-scan", daemon=True)
            self.thread.start()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=2)
        if self.lease and (not self.thread or not self.thread.is_alive()):
            self.lease.close()
            self.lease = None

    def decode(self, row):
        result = dict(row)
        result["request"] = json.loads(result["request"])
        result["result"] = json.loads(result["result"]) if result["result"] else None
        return result

    def get(self, ident):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM scan_jobs WHERE id=?", (ident,)).fetchone()
            if row is None:
                raise KeyError(ident)
            result = self.decode(row)
            result["events"] = [
                dict(r)
                for r in db.execute(
                    "SELECT stage,percent,detail,created_at FROM scan_job_events "
                    "WHERE job_id=? ORDER BY id",
                    (ident,),
                )
            ]
        return result

    def latest(self):
        with self.store.connect() as db:
            row = db.execute("SELECT id FROM scan_jobs ORDER BY created_at DESC LIMIT 1").fetchone()
        return self.get(row[0]) if row else None

    def submit(self, request: SearchRequest, mode="live"):
        if mode == "live" and not ready(self.store):
            raise ValueError("Добавьте ключи поиска и модель в подключениях.")
        payload = request.model_dump_json()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute(
                "SELECT * FROM scan_jobs WHERE status IN ('queued','running')"
            ).fetchone()
            if active:
                if active["request"] == payload and active["mode"] == mode:
                    return self.decode(active)
                raise ValueError(
                    "Другой поиск ещё выполняется. Дождитесь завершения или остановите его."
                )
            ident = uuid4().hex
            stamp = now()
            db.execute(
                "INSERT INTO scan_jobs(id,mode,request,status,stage,detail,created_at,updated_at) "
                "VALUES(?,?,?,'queued','queued',?,?,?)",
                (ident, mode, payload, "Поиск поставлен в очередь", stamp, stamp),
            )
        self.wake.set()
        return self.get(ident)

    def cancel(self, ident):
        with self.store.connect() as db:
            db.execute(
                "UPDATE scan_jobs SET cancel_requested=1,updated_at=?, "
                "status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END "
                "WHERE id=? AND status IN ('queued','running')",
                (now(), ident),
            )
        return self.get(ident)

    def progress(self, ident, stage, percent, detail):
        with self.store.connect() as db:
            db.execute(
                "UPDATE scan_jobs SET stage=?,percent=?,detail=?,updated_at=? WHERE id=?",
                (stage, percent, detail, now(), ident),
            )
            db.execute(
                "INSERT INTO scan_job_events(job_id,stage,percent,detail,created_at) "
                "VALUES(?,?,?,?,?)",
                (ident, stage, percent, detail, now()),
            )

    def cancelled(self, ident):
        return self.stop.is_set() or bool(self.get(ident)["cancel_requested"])

    def execute(self, job):
        ident = job["id"]
        with self.store.connect() as db:
            changed = db.execute(
                "UPDATE scan_jobs SET status='running',updated_at=? WHERE id=? AND status='queued'",
                (now(), ident),
            ).rowcount
        if not changed:
            return
        status, result, error = "failed", None, None
        try:
            request = SearchRequest.model_validate_json(job["request"])
            kwargs = dict(
                progress=lambda stage, percent, detail: self.progress(
                    ident, stage, percent, detail
                ),
                cancelled=lambda: self.cancelled(ident),
            )
            if job["mode"] == "demo":
                from radar.web.demo import run_demo

                result = run_demo(self.store, request, **kwargs)
            else:
                result = discover(self.store, request, **kwargs)
            status = result["status"]
        except ScanCancelled:
            status = "interrupted" if self.stop.is_set() else "cancelled"
            error = "Поиск остановлен. Уже сохранённые источники и карточки остаются доступны."
        except ValueError as exc:
            error = str(exc)
        except Exception:
            logging.getLogger(__name__).exception("Scan worker failed")
            error = "Не удалось завершить поиск. Сохранённые шаги доступны для повторного запуска."
        with self.store.connect() as db:
            db.execute(
                "UPDATE scan_jobs SET status=?,result=?,error=?,updated_at=? WHERE id=?",
                (status, json.dumps(result) if result is not None else None, error, now(), ident),
            )

    def loop(self):
        while not self.stop.is_set():
            with self.store.connect() as db:
                row = db.execute(
                    "SELECT * FROM scan_jobs WHERE status='queued' ORDER BY created_at LIMIT 1"
                ).fetchone()
            if row:
                self.execute(dict(row))
            else:
                self.wake.wait(1)
                self.wake.clear()
