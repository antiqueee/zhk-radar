import csv
import io
import json
import secrets
import tempfile
import zipfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from starlette.middleware.trustedhost import TrustedHostMiddleware

from radar.briefs.offers import load_offers
from radar.intake import read_workbook
from radar.pipeline.evidence import validate_quote
from radar.web import advanced, completion, operations, stoplist
from radar.web.auth import COOKIE, Auth, Login
from radar.web.benchmark import (
    BenchmarkModels,
    BenchmarkRequest,
    BenchmarkRunner,
    Review,
    save_models,
)
from radar.web.chat_analysis import analyze
from radar.web.chats import ChatImport, aggregates, ingest_chat
from radar.web.configuration import (
    KEYS,
    SETTINGS_LOCK,
    SettingsUpdate,
    atomic_write,
    configuration,
    model_config,
    save_settings,
)
from radar.web.discovery import SearchRequest
from radar.web.dossiers import dossier
from radar.web.jobs import JobRunner
from radar.web.maintenance import Maintenance
from radar.web.operations import OperationsSettings, WeeklyScheduler
from radar.web.pdf import render_pdf
from radar.web.store import STATUSES, Store, now
from radar.web.telegram import TelegramCode, TelegramLink, TelegramLogin, TelegramReader


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    status: Literal["new", "review", "inwork", "proposal", "meeting", "closed", "later", "rejected"]
    note: str = Field(default="", max_length=3000)
    stage: int = Field(default=0, ge=0, le=4)
    situation: Literal["сроки", "приёмка", "после_заселения"] = "сроки"
    topics: list[str] = Field(default_factory=list, max_length=15)
    refusal_reason: str = Field(default="", max_length=500)


class JobRequest(SearchRequest):
    mode: Literal["live", "demo"] = "live"


class Proof(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=160)
    url: HttpUrl
    quote: str = Field(min_length=1, max_length=500)
    source_text: str = Field(min_length=1, max_length=100000)


def create_app(root: Path | None = None):
    root = root or Path.cwd()
    store = Store(root)
    auth = Auth(store)
    jobs = JobRunner(store)
    benchmark = BenchmarkRunner(store)
    maintenance = Maintenance(store)

    @asynccontextmanager
    async def lifespan(app):
        jobs.start()
        maintenance.start()
        scheduler.start()
        benchmark.start()
        tasks.start()
        daily.start()
        yield
        workers = [daily, tasks, maintenance, scheduler, benchmark]
        for worker in [*workers, jobs]:
            if hasattr(worker, "stop"):
                worker.stop.set()
        for worker in workers:
            worker.close()
        # Keep the process lease if an external request outlives graceful shutdown.
        jobs.close(release=not any(w.thread and w.thread.is_alive() for w in workers))

    app = FastAPI(title="Мегаполис Интеледженс", docs_url="/api/docs", lifespan=lifespan)
    app.state.jobs = jobs
    app.state.store = store
    app.state.maintenance = maintenance
    completion.install(app, store)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=auth.config["hosts"])

    @app.middleware("http")
    async def local_guard(request: Request, call_next):
        identity = auth.session(request.cookies.get(COOKIE))
        request.state.identity = identity
        path = request.url.path
        if auth.config["enabled"] and path.startswith("/api/") and path != "/api/auth/login":
            if not identity:
                return JSONResponse({"detail": "Войдите в рабочий кабинет"}, status_code=401)
            admin_paths = (
                "/api/settings",
                "/api/operations",
                "/api/telegram/login",
                "/api/telegram/confirm",
                "/api/telegram/session",
                "/api/benchmark/models",
                "/api/sheets",
                "/api/maintenance",
                "/api/cases",
                "/api/evaluations",
                "/api/automation",
                "/api/budget",
                "/api/external-operations",
            )
            if identity["role"] != "admin" and path.startswith(admin_paths):
                return JSONResponse({"detail": "Доступно администратору"}, status_code=403)
            if (
                identity["role"] == "viewer"
                and request.method not in {"GET", "HEAD"}
                and path != "/api/auth/logout"
            ):
                return JSONResponse({"detail": "Доступ только для просмотра"}, status_code=403)
        if request.method in {"POST", "PUT", "DELETE", "PATCH"}:
            try:
                size = int(request.headers.get("content-length", "0"))
            except ValueError:
                return JSONResponse({"detail": "Некорректный размер запроса"}, status_code=400)
            if size > 12 * 1024 * 1024:
                return JSONResponse({"detail": "Максимальный запрос — 12 МБ"}, status_code=413)
            if request.headers.get("X-Radar-Local") != "1":
                return JSONResponse(
                    {"detail": "Разрешены действия из локального интерфейса"}, status_code=403
                )
            origin = request.headers.get("origin")
            allowed_origin = auth.config["origin"] or str(request.base_url).rstrip("/")
            if (origin and origin != allowed_origin) or (auth.config["enabled"] and not origin):
                return JSONResponse({"detail": "Недопустимый источник запроса"}, status_code=403)
        response = await call_next(request)
        auth.audit(identity, request, response.status_code)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Do not echo request inputs: settings bodies contain credentials.
        return JSONResponse(
            {"detail": "Проверьте обязательные поля и формат данных"}, status_code=422
        )

    @app.post("/api/auth/login")
    def login(data: Login, request: Request):
        token = auth.login(data, request.client.host if request.client else "unknown")
        response = JSONResponse({"signed_in": True})
        response.set_cookie(
            COOKIE, token, max_age=8 * 3600, secure=True, httponly=True, samesite="strict", path="/"
        )
        return response

    @app.post("/api/auth/logout")
    def logout(request: Request):
        auth.logout(request.cookies.get(COOKIE))
        response = JSONResponse({"signed_out": True})
        response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="strict")
        return response

    @app.get("/api/session")
    def current_session(request: Request):
        return request.state.identity

    @app.get("/healthz")
    def health():
        with store.connect() as db:
            db.execute("SELECT 1").fetchone()
        return {"status": "ok"}

    @app.put("/api/settings")
    def update_settings(data: SettingsUpdate):
        save_settings(root, data)
        return {"saved": True}

    @app.get("/api/benchmark/config")
    def benchmark_config():
        return benchmark.config()

    @app.put("/api/benchmark/models")
    def benchmark_models(data: BenchmarkModels):
        save_models(root, data)
        return benchmark.config()

    @app.post("/api/benchmark", status_code=202)
    def benchmark_start(data: BenchmarkRequest):
        return benchmark.submit(data)

    @app.get("/api/benchmark/latest")
    def benchmark_latest():
        return benchmark.get()

    @app.get("/api/benchmark/{ident}")
    def benchmark_result(ident: str):
        return benchmark.get(ident)

    @app.put("/api/benchmark/{ident}/review")
    def benchmark_review(ident: str, data: Review):
        return benchmark.review(ident, data)

    @app.post("/api/search", status_code=202)
    def search(data: JobRequest):
        request = SearchRequest.model_validate(data.model_dump(exclude={"mode"}))
        return jobs.submit(request, data.mode)

    @app.get("/api/search/readiness")
    def search_readiness():
        cfg = configuration(root)
        return {
            "ready": bool(model_config(root)["extract"].get("model"))
            and all(
                cfg.get(k)
                for k in ["YANDEX_SEARCH_API_KEY", "YANDEX_FOLDER_ID", "OPENROUTER_API_KEY"]
            )
        }

    @app.get("/api/jobs/latest")
    def latest_job():
        return jobs.latest()

    @app.get("/api/jobs/{ident}")
    def get_job(ident: str):
        return jobs.get(ident)

    @app.post("/api/jobs/{ident}/cancel")
    def cancel_job(ident: str):
        return jobs.cancel(ident)

    @app.get("/api/sources/{ident}")
    def source_page(ident: str):
        with store.connect() as db:
            row = db.execute("SELECT payload FROM source_pages WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise KeyError(ident)
        return json.loads(row[0])

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": "Запись не найдена"}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/api/stoplist")
    def stop_entries():
        return stoplist.entries(store)

    @app.post("/api/stoplist")
    def stop_add(data: stoplist.StopEntry, request: Request):
        return stoplist.add(store, data, request.state.identity["username"])

    @app.delete("/api/stoplist/{ident}")
    def stop_remove(ident: str):
        stoplist.remove(store, ident)
        return {"removed": True}

    @app.get("/api/records")
    def records(dataset: Literal["imported", "demo", "web"] = "imported"):
        excluded = stoplist.entries(store)
        from radar.web import workflow

        workflow.initialize(store)
        with store.connect() as db:
            scores = {
                r["record_id"]: {**json.loads(r["payload"]), "updated_at": r["updated_at"]}
                for r in db.execute("SELECT * FROM candidate_scores")
            }
        rows = [
            {**r, "blocked": stoplist.match(r, excluded), "ranking": scores.get(r["id"])}
            for r in store.records(dataset)
        ]
        return sorted(rows, key=lambda r: -(r["ranking"] or {}).get("rank_score", 0))

    @app.get("/api/records/{ident}")
    def record(ident: str):
        record = store.get(ident)
        return {
            **record,
            "dossier": dossier(store, ident),
            "blocked": stoplist.blocked(store, record),
        }

    @app.put("/api/records/{ident}")
    def update(ident: str, data: Decision):
        store.get(ident)
        topics = {t for o in load_offers(root / "config/offers.yaml").offers for t in o.pain_topics}
        if not set(data.topics) <= topics:
            raise ValueError("Неизвестная тема")
        try:
            return store.update(ident, data)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/records/{ident}/evidence")
    def evidence(ident: str, data: Proof):
        validate_quote(data.source_text, data.quote)
        return store.add_evidence(ident, data)

    @app.get("/api/offers")
    def offers():
        return load_offers(root / "config/offers.yaml").model_dump()

    def author_salt():
        with SETTINGS_LOCK:
            path = root / ".env"
            configured = configuration(root).get("AUTHOR_HASH_SALT")
            if configured:
                return configured
            salt = secrets.token_hex(32)
            previous = path.read_text() if path.exists() else ""
            atomic_write(path, previous + f"\nAUTHOR_HASH_SALT={salt}\n")
            return salt

    @app.post("/api/chats/import")
    def chat_import(batch: ChatImport):
        known = {
            t
            for offer in load_offers(root / "config/offers.yaml").offers
            for t in offer.pain_topics
        }
        if any(m.topic not in known | {"не_размечено"} for m in batch.messages):
            raise ValueError(
                "Неизвестные темы в выгрузке; используйте коды библиотеки или не_размечено"
            )
        return ingest_chat(store, batch, author_salt())

    telegram = TelegramReader(store, author_salt)
    app.state.telegram = telegram
    tasks = advanced.install(app, store)
    from radar.web.automation import DailyWorker

    daily = DailyWorker(store, telegram)
    scheduler = WeeklyScheduler(store, jobs, telegram)
    app.state.scheduler = scheduler

    @app.get("/api/operations")
    def operation_status():
        return {
            "settings": operations.settings(store),
            "usage": operations.usage(store),
            "history": scheduler.history(),
            "scheduler_error": scheduler.last_error,
        }

    @app.put("/api/operations")
    def operation_settings(data: OperationsSettings):
        return operations.save(store, data)

    @app.get("/api/telegram/status")
    def telegram_status():
        return telegram.status()

    @app.post("/api/telegram/login")
    def telegram_login(data: TelegramLogin):
        return telegram.login(data.phone)

    @app.post("/api/telegram/confirm")
    def telegram_confirm(data: TelegramCode):
        return telegram.confirm(data)

    @app.delete("/api/telegram/session")
    def telegram_disconnect():
        return telegram.disconnect()

    @app.get("/api/records/{ident}/telegram")
    def telegram_links(ident: str):
        store.get(ident)
        return telegram.links(ident)

    @app.post("/api/records/{ident}/telegram")
    def telegram_add(ident: str, data: TelegramLink):
        return telegram.add(ident, data)

    @app.delete("/api/records/{ident}/telegram")
    def telegram_remove(ident: str, data: TelegramLink):
        return telegram.remove(ident, data)

    @app.post("/api/records/{ident}/telegram/sync")
    def telegram_sync(ident: str, data: TelegramLink):
        return telegram.sync(ident, data.url)

    @app.post("/api/records/{ident}/chats/analyze")
    def chat_analyze(ident: str):
        return analyze(store, ident)

    @app.get("/api/chats/example")
    def chat_example():
        return {
            "record_id": "demo-1",
            "chat_id": "demo-open-chat",
            "title": "Демо: открытый чат",
            "access": "public",
            "messages": [
                {
                    "id": str(i),
                    "author_id": f"demo-author-{i % 9}",
                    "sent_at": (
                        datetime.now(UTC) - timedelta(days=i % 7, hours=i % 12)
                    ).isoformat(),
                    "text": "Учебный пример: сотрудники называют разные сроки передачи.",
                    "topic": "разные_ответы" if i % 3 else "выдача_ключей",
                    "sentiment": -1 if i % 4 else 0,
                }
                for i in range(42)
            ],
        }

    @app.post("/api/chats/demo")
    def chat_demo():
        return chat_import(ChatImport.model_validate(chat_example()))

    @app.get("/api/chats")
    def chats(dataset: Literal["imported", "demo", "web"] = "imported"):
        result = []
        for record in store.records(dataset):
            stats = aggregates(store, record["id"])
            if stats["chats"]:
                result.append({**stats, "name": record["name"], "developer": record["developer"]})
        return result

    @app.post("/api/records/{ident}/brief")
    def build_brief(ident: str):
        from radar.web.brief_service import build

        return build(store, ident)

    @app.get("/api/briefs")
    def briefs(dataset: Literal["imported", "demo", "web"] = "imported"):
        with store.connect() as db:
            rows = db.execute(
                "SELECT b.* FROM briefs b JOIN records r ON r.id=b.record_id "
                "WHERE r.dataset=? ORDER BY b.id DESC",
                (dataset,),
            )
            return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    @app.get("/api/briefs/{brief_id}/pdf")
    def pdf(brief_id: int):
        with store.connect() as db:
            row = db.execute(
                "SELECT payload,version,created_at FROM briefs WHERE id=?", (brief_id,)
            ).fetchone()
        if not row:
            raise HTTPException(404, "Расклад не найден")
        return Response(
            render_pdf({**json.loads(row[0]), "version": row[1], "created_at": row[2]}),
            media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="radar-brief-{brief_id}.pdf"'},
        )

    @app.post("/api/import")
    async def import_file(file: UploadFile):
        if not file.filename or not file.filename.lower().endswith(".xlsx"):
            raise ValueError("Нужен файл .xlsx")
        content = await file.read(5 * 1024 * 1024 + 1)
        if len(content) > 5 * 1024 * 1024:
            raise ValueError("Максимальный размер — 5 МБ")
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as z:
                if sum(i.file_size for i in z.infolist()) > 30 * 1024 * 1024:
                    raise ValueError("Слишком большой распакованный файл")
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / Path(file.filename).name
                path.write_bytes(content)
                book = read_workbook(path)
                count = store.ingest(book)
        except (zipfile.BadZipFile, KeyError) as exc:
            raise ValueError("Некорректная книга Excel") from exc
        return {"added": count, "found": len(book["candidates"]), "sheets": len(book["sheets"])}

    @app.get("/api/export")
    def export(dataset: Literal["imported", "demo", "web"] = "imported"):
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["Объект", "Девелопер", "Регион", "Статус", "Источник", "Проверка"])
        for r in store.records(dataset):
            values = [
                r["name"],
                r["developer"],
                r["region"],
                STATUSES[r["status"]],
                r["file"],
                r["verification"],
            ]
            writer.writerow(["'" + v if v.startswith(("=", "+", "-", "@")) else v for v in values])
        return Response(
            "\ufeff" + out.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="radar-registry.csv"'},
        )

    @app.post("/api/run")
    def run(dataset: Literal["imported", "demo", "web"] = "imported"):
        records = store.records(dataset)
        steps = [
            {
                "name": "Локальные материалы",
                "status": "done",
                "detail": f"{len(records)} карточек доступно",
            },
            {
                "name": "Контроль происхождения",
                "status": "done",
                "detail": "Исходные классификации не заменяют проверку",
            },
            {
                "name": "Новые данные из интернета",
                "status": "skipped",
                "detail": "Сборщики ещё не подключены",
            },
            {"name": "Чаты и модели", "status": "skipped", "detail": "Без внешних вызовов"},
            {
                "name": "Доставка",
                "status": "skipped",
                "detail": "Доступна локальная выгрузка и PDF",
            },
        ]
        payload = {"status": "partial", "steps": steps, "cost_usd": 0, "external_requests": 0}
        with store.connect() as db:
            db.execute(
                "INSERT INTO runs(dataset,payload,created_at) VALUES(?,?,?)",
                (dataset, json.dumps(payload), now()),
            )
        return payload

    @app.get("/api/runs")
    def runs(dataset: Literal["imported", "demo", "web"] = "imported"):
        with store.connect() as db:
            return [
                {**dict(r), "payload": json.loads(r["payload"])}
                for r in db.execute(
                    "SELECT * FROM runs WHERE dataset=? ORDER BY id DESC LIMIT 20", (dataset,)
                )
            ]

    @app.get("/api/settings")
    def settings():
        cfg = configuration(root)
        selected_model = model_config(root)["extract"].get("model", "")
        services = [
            ("OpenRouter", ["OPENROUTER_API_KEY"], "Извлечение фактов и текст расклада"),
            (
                "Yandex Search",
                ["YANDEX_SEARCH_API_KEY", "YANDEX_FOLDER_ID"],
                "Поиск новых сигналов",
            ),
            ("Telegram-бот", ["TG_BOT_TOKEN", "TG_GROUP_ID"], "Дайджест и уведомления"),
            (
                "Читатель Telegram",
                ["TG_API_ID", "TG_API_HASH"],
                "Открытые чаты с отдельного аккаунта",
            ),
            ("Google Sheets", ["GOOGLE_SA_JSON", "SHEET_ID"], "Синхронизация реестра"),
        ]
        return {
            "mode": "server" if auth.config["enabled"] else "local",
            "fields": {key: bool(cfg.get(key)) for key in KEYS},
            "model": selected_model,
            "review_model": model_config(root).get("review", {}).get("model", ""),
            "author_model": model_config(root).get("author", {}).get("model", ""),
            "search_ready": bool(selected_model)
            and all(
                cfg.get(k)
                for k in ["YANDEX_SEARCH_API_KEY", "YANDEX_FOLDER_ID", "OPENROUTER_API_KEY"]
            ),
            "storage": "SQLite · data/local/workspace.sqlite3",
            "services": [
                {
                    "name": n,
                    "keys": keys,
                    "purpose": p,
                    "configured": all(
                        bool(
                            cfg.get(k)
                            or (k == "GOOGLE_SA_JSON" and cfg.get("GOOGLE_SA_CREDENTIALS"))
                        )
                        for k in keys
                    ),
                    "connected": False,
                }
                for n, keys, p in services
            ],
        }

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def home(request: Request):
        return FileResponse(
            static
            / (
                "login.html"
                if auth.config["enabled"] and not request.state.identity
                else "index.html"
            )
        )

    return app
