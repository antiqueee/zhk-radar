"""Web endpoints for autonomous research, authored proposals and public chat suggestions."""

import json
from typing import Literal

from fastapi import Request
from fastapi.responses import Response
from pydantic import BaseModel

from radar.web import (
    author,
    automation,
    cases,
    chat_workflow,
    client_pdf,
    evaluation,
    external_ops,
    extra_sources,
    money,
    screening,
    workflow,
)
from radar.web.tasks import TaskRunner


class TaskRequest(BaseModel):
    kind: Literal["research", "author", "chat_discovery", "extra_sources"]


class ChatDecision(BaseModel):
    url: str
    accepted: bool


def install(app, store):
    tasks = TaskRunner(
        store,
        {
            "extra_sources": lambda ident, run: extra_sources.collect(store, ident, run),
            "research": lambda ident, run: workflow.research(store, ident, run),
            "author": lambda ident, run: author.generate(store, ident, run),
            "chat_discovery": lambda ident, run: chat_workflow.discover_chats(store, ident, run),
        },
    )
    app.state.tasks = tasks

    @app.post("/api/records/{ident}/tasks", status_code=202)
    def start(ident: str, data: TaskRequest):
        return tasks.submit(data.kind, ident)

    @app.get("/api/tasks/latest")
    def latest():
        return tasks.get()

    @app.get("/api/tasks/{ident}")
    def status(ident: str):
        return tasks.get(ident)

    @app.get("/api/records/{ident}/intelligence")
    def intelligence(ident: str):
        value = workflow.score(store, ident)
        with store.connect() as db:
            row = db.execute(
                "SELECT payload FROM research_runs WHERE record_id=? "
                "ORDER BY created_at DESC LIMIT 1",
                (ident,),
            ).fetchone()
        return {
            "score": value,
            "research": json.loads(row[0]) if row else None,
            "chat_candidates": chat_workflow.candidates(store, ident),
            "dynamics": chat_workflow.dynamics(store, ident),
            "screening": screening.latest(store, ident),
        }

    @app.post("/api/records/{ident}/chat-candidates")
    def accept_chat(ident: str, data: ChatDecision):
        return chat_workflow.decision(store, ident, data.url, data.accepted, app.state.telegram)

    @app.post("/api/chat-candidates/inspect")
    def inspect_chat(data: ChatDecision):
        return app.state.telegram.inspect(data.url)

    @app.get("/api/briefs/{ident}/client-preview")
    def preview(ident: int):
        return Response(client_pdf.export(store, ident), media_type="application/pdf")

    @app.get("/api/briefs/{ident}/client-pdf")
    def client(ident: int):
        return Response(
            client_pdf.export(store, ident, draft=False),
            media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="megapolis-proposal-{ident}.pdf"'},
        )

    @app.get("/api/records/{ident}/extra-sources")
    def extra_status(ident: str):
        return extra_sources.status(store, ident)

    @app.put("/api/records/{ident}/extra-sources")
    def extra_bind(ident: str, data: extra_sources.Binding):
        return extra_sources.bind(store, ident, data)

    @app.get("/api/evaluations")
    def evaluations():
        return evaluation.history(store)

    @app.post("/api/evaluations")
    def evaluation_save(data: evaluation.Evaluation):
        return evaluation.save(store, data)

    @app.get("/api/evaluations/example")
    def evaluation_example():
        return evaluation.example()

    @app.get("/api/cases")
    def case_list():
        return cases.entries(store)

    @app.post("/api/cases")
    def case_save(data: cases.Case, request: Request):
        return cases.save(store, data)

    @app.get("/api/cases/example")
    def case_example():
        return {
            "title": "Замените названием реального кейса",
            "situation": "сроки",
            "topics": ["разные_ответы"],
            "description": "Опишите подтверждённую ситуацию и работу агентства",
            "result": "Опишите результат без вымышленных показателей",
            "evidence": "Укажите документ или способ проверки результата",
            "approved": False,
            "client_use": False,
        }

    @app.delete("/api/cases/{ident}")
    def case_remove(ident: str):
        cases.initialize(store)
        with store.connect() as db:
            db.execute("DELETE FROM agency_cases WHERE id=?", (ident,))
        return {"removed": True}

    @app.get("/api/automation")
    def automation_status():
        return automation.status(store)

    @app.put("/api/automation")
    def automation_save(data: automation.AutomationSettings):
        return automation.save(store, data)

    @app.get("/api/budget")
    def budget_status():
        return money.summary(store)

    @app.put("/api/budget")
    def budget_save(data: money.MoneySettings):
        return money.save(store, data)

    @app.post("/api/budget/price-history")
    def price_history():
        money.initialize(store)
        cfg = money.settings(store)
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute(
                "SELECT id,service FROM api_calls WHERE reserved_usd IS NULL"
            ).fetchall():
                amount = (
                    cfg.model_reserve_usd
                    if ("model" in row["service"] or "extract" in row["service"])
                    else cfg.search_reserve_usd
                )
                db.execute("UPDATE api_calls SET reserved_usd=? WHERE id=?", (amount, row["id"]))
        return money.summary(store)

    @app.get("/api/external-operations")
    def external_status():
        return external_ops.status(store)

    @app.put("/api/external-operations")
    def external_save(data: external_ops.ExternalSettings):
        return external_ops.save(store, data)

    @app.post("/api/external-operations/backup")
    def external_backup():
        return external_ops.upload_backup(store)

    @app.post("/api/external-operations/encryption-key")
    def encryption_key():
        from cryptography.fernet import Fernet

        from radar.web.configuration import SETTINGS_LOCK, configuration, write_secrets

        with SETTINGS_LOCK:
            if configuration(store.root).get("BACKUP_ENCRYPTION_KEY"):
                raise ValueError("Ключ уже сохранён. Для замены введите новый ключ в подключениях")
            key = Fernet.generate_key().decode()
            write_secrets(store.root, {"BACKUP_ENCRYPTION_KEY": key})
        return {
            "key": key,
            "note": "Сохраните в менеджере паролей. Без ключа восстановить внешнюю копию нельзя.",
        }

    return tasks
