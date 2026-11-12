import io
import json
import shutil
import tarfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pypdf import PdfReader

from radar.web import (
    author,
    automation,
    cases,
    chat_workflow,
    client_pdf,
    external_ops,
    money,
    review,
    workflow,
)
from radar.web.app import create_app
from radar.web.brief_service import build, payload_for
from radar.web.chats import ChatImport, ingest_chat
from radar.web.configuration import SettingsUpdate, save_settings, write_secrets
from radar.web.demo import run_demo
from radar.web.discovery import SearchRequest
from radar.web.operations import BudgetExceeded, reserve_call, usage
from radar.web.research_answers import Answer, Answers, validate_answers
from radar.web.store import Store


@pytest.fixture
def store(tmp_path):
    shutil.copytree("config", tmp_path / "config")
    return Store(tmp_path)


def real(store):
    with store.connect() as db:
        db.execute("UPDATE records SET dataset='web' WHERE id='demo-1'")
    c = TestClient(create_app(store.root), headers={"X-Radar-Local": "1"})
    c.post(
        "/api/records/demo-1/evidence",
        json={
            "title": "Тестовый источник",
            "url": "https://example.org/proof",
            "quote": "Сотрудники называют разные сроки передачи квартир.",
            "source_text": "Сотрудники называют разные сроки передачи квартир.",
        },
    )
    return c


def test_source_class_exact_host_and_research_exclusion(store):
    assert (
        workflow.source_class(store, {"source_url": "https://rbc.ru/a", "source_kind": "page"})
        == "B"
    )
    assert (
        workflow.source_class(
            store, {"source_url": "https://rbc.ru.evil.test/a", "source_kind": "page"}
        )
        == "C"
    )
    assert (
        workflow.source_class(store, {"source_url": "https://mos.ru/a", "source_kind": "snippet"})
        == "C"
    )
    run = run_demo(store, SearchRequest(region="moscow"), lambda *args: None, lambda: False)
    ident = run["record_ids"][0]
    with store.connect() as db:
        db.execute("UPDATE records SET status='later' WHERE id=?", (ident,))
    result = workflow.score(store, ident)
    assert result["excluded"] and not result["needs_research"] and result["rank_score"] == 0


def test_full_cycle_preserves_initial_results_on_research_budget_limit(store):
    run = run_demo(store, SearchRequest(region="moscow"), lambda *args: None, lambda: False)
    # Fixture dates intentionally set to today's date, never fabricate freshness in implementation.
    with store.connect() as db:
        for row in db.execute("SELECT id,payload FROM record_signals").fetchall():
            data = json.loads(row["payload"])
            data["event_date"] = datetime.now(UTC).date().isoformat()
            db.execute(
                "UPDATE record_signals SET payload=? WHERE id=?", (json.dumps(data), row["id"])
            )

    def limited(*args, **kwargs):
        raise BudgetExceeded("budget")

    result = workflow.complete_scan(store, run, "run", researcher=limited)
    assert result["status"] == "partial" and result["record_ids"] == run["record_ids"]
    assert result["errors"][-1]["stage"] == "research"
    assert result["shortlist"]


def test_research_answers_reject_other_building_and_invented_quotes():
    record = {"name": "Учебный берег", "building": "корпус 3"}
    text = "Учебный берег корпус 4: официальный ответ девелопера опубликован."
    docs = [{"text": text, "url": "https://example.org", "source_kind": "page", "source_id": "a"}]
    data = Answers(answers=[Answer(question="developer_response", quote=text, source_index=0)])
    with pytest.raises(ValueError, match="корпусу"):
        validate_answers(data, record, docs)
    data.answers[0] = data.answers[0].model_copy(update={"quote": text.replace("4", "3")})
    with pytest.raises(ValueError, match="цитатой"):
        validate_answers(data, record, docs)


def configure_author(store):
    save_settings(
        store.root,
        SettingsUpdate(
            values={"OPENROUTER_API_KEY": "fixture"},
            model="test/extract",
            author_model="test/author",
            review_model="test/critic",
        ),
    )


def valid_author(payload, sources):
    return author.Authored(
        products=[
            author.ProductText(
                code=p["code"],
                rationale=(
                    "Предлагаем согласовать ответы по зафиксированным вопросам о передаче квартир."
                ),
                tasks=[
                    "Собрать противоречивые ответы и согласовать общую позицию.",
                    "Назначить ответственного за обновление материалов по корпусу.",
                    "Проверить применение согласованных ответов в клиентском сервисе.",
                ],
                result="Согласованные материалы и порядок обновления ответов для команды.",
                measurement="Сравнить частоту противоречивых ответов до и после запуска.",
                source_ids=[sources[0]["id"]],
            )
            for p in payload["report"]["products"]
        ]
    )


def test_author_one_repair_and_content_cache(store):
    real(store)
    configure_author(store)
    calls = []

    def writer(client, cfg, model, payload, sources, matches, criticism):
        calls.append("writer")
        value = valid_author(payload, sources)
        if criticism:
            value.products[0].tasks[0] = (
                "Сверить спорные ответы с подтверждёнными материалами по корпусу."
            )
        return value

    def critic(client, cfg, model, payload, sources):
        calls.append("critic")
        if calls.count("critic") == 1:
            return review.ReviewResult(
                verdict="revise",
                issues=[
                    review.Issue(
                        block="tasks",
                        severity="warning",
                        reason="Уточните сверку исходных материалов",
                    )
                ],
            )
        return review.ReviewResult(verdict="passed", issues=[])

    first = author.generate(store, "demo-1", "test", writer, critic, client=object())
    second = author.generate(store, "demo-1", "test2", writer, critic, client=object())
    assert first["id"] == second["id"] and calls == ["writer", "critic", "writer", "critic"]
    assert first["payload"]["author"]["status"] == "reviewed"
    assert not review.operator_approved(store, first["id"])
    assert usage(store)["calls"] == 4
    assert build(store, "demo-1")["id"] == first["id"]
    with store.connect() as db:
        db.execute("UPDATE records SET note='Новое уточнение' WHERE id='demo-1'")
    assert "author" not in build(store, "demo-1")["payload"]


def test_bad_author_does_not_publish_and_does_not_recharge(store):
    real(store)
    configure_author(store)

    def writer(client, cfg, model, payload, sources, matches, criticism):
        value = valid_author(payload, sources)
        value.products[0].result = "Гарантируем сокращение жалоб на 987654 процентов за неделю."
        return value

    for _ in range(2):
        with pytest.raises(ValueError, match="число"):
            author.generate(store, "demo-1", "test", writer, client=object())
    assert usage(store)["calls"] == 1
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM briefs").fetchone()[0] == 0


def test_client_pdf_approval_freshness_and_allowlist(store):
    c = real(store)
    brief = build(store, "demo-1")
    with pytest.raises(ValueError, match="одобрить"):
        client_pdf.export(store, brief["id"], draft=False)
    review.approve(
        store,
        brief["id"],
        review.OperatorReview(approved=True, note="Проверены источники и границы корпуса"),
        "operator",
    )
    pdf = client_pdf.export(store, brief["id"], draft=False)
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
    assert "Предложение для обсуждения" in text
    for hidden in [
        "Острота",
        "Продаваемость",
        "СТОП",
        "Сотрудники называют разные сроки",
        "Источник:",
    ]:
        assert hidden not in text
    r = store.get("demo-1")
    data = {k: r[k] for k in ["revision", "status", "note", "stage", "situation", "topics"]}
    data["note"] = "Изменившиеся исходные сведения"
    c.put("/api/records/demo-1", json=data)
    with pytest.raises(ValueError, match="изменились"):
        client_pdf.export(store, brief["id"], draft=False)


def test_client_projection_replaces_authored_chat_quote_with_playbook(store):
    payload = payload_for(store, "demo-1")
    secret = "Жители из квартиры Иванова обсуждают тайную встречу в четверг около подъезда"
    payload["chat_stats"] = {"examples": [{"text": secret}]}
    payload["report"]["products"][0]["tasks"] = [secret]
    result = client_pdf.projection(payload, set())
    assert secret not in json.dumps(result, ensure_ascii=False)
    assert "evidence" not in result and "assessment" not in result


def test_cases_require_approval_and_same_situation(store):
    data = dict(
        title="Подтверждённый кейс",
        situation="сроки",
        topics=["разные_ответы"],
        description="Реальная работа команды по согласованию ответов.",
        result="Подготовлена единая база ответов.",
        evidence="Внутренний акт завершения работ",
    )
    cases.save(store, cases.Case(**data))
    assert not cases.matching(store, payload_for(store, "demo-1"))
    cases.save(store, cases.Case(**data, approved=True))
    matched = cases.matching(store, payload_for(store, "demo-1"))
    assert len(matched) == 1 and not matched[0]["client_use"]


def test_chat_suggestions_never_auto_connect_and_cache(store):
    real(store)
    write_secrets(store.root, {"YANDEX_SEARCH_API_KEY": "test", "YANDEX_FOLDER_ID": "test"})
    calls = []

    def search(*args):
        assert args[-1] == "all"  # Chat creation date must not restrict 30-day history reads.
        calls.append(1)
        return [
            {
                "url": "https://t.me/public_chat",
                "title": "Чат похожего ЖК",
                "text": "https://t.me/public_chat",
            }
        ]

    result = chat_workflow.discover_chats(store, "demo-1", "run", client=object(), searcher=search)
    chat_workflow.discover_chats(store, "demo-1", "run", client=object(), searcher=search)
    assert len(calls) == 1 and result["candidates"][0]["status"] == "suggested"
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM telegram_links").fetchone()[0] == 0


def test_chat_dynamics_thresholds_and_alert_idempotency(store):
    clock = datetime.now(UTC)
    batch = ChatImport(
        record_id="demo-1",
        chat_id="c",
        title="Учебный чат",
        access="public",
        messages=[
            {
                "id": str(i),
                "author_id": str(i % 10),
                "text": "Учебный пример без контактов",
                "sent_at": clock - timedelta(days=10 if i < 10 else 2),
                "topic": "разные_ответы",
                "sentiment": 0 if i < 10 else -1,
            }
            for i in range(35)
        ],
    )
    ingest_chat(store, batch, "salt")
    value = chat_workflow.record_alerts(store, "demo-1", clock)
    chat_workflow.record_alerts(store, "demo-1", clock)
    assert len(value["signals"]) == 2
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM local_alerts").fetchone()[0] == 2


def test_money_atomic_reservation_actual_charge_and_unknown_failure(store):
    reserve_call(store, "setup", "search")
    money.settle_response({"usage": {"cost": 0.05}})
    money.save(store, money.MoneySettings(enabled=True, monthly_usd=0.45, search_reserve_usd=0.1))

    def attempt(i):
        try:
            reserve_call(store, str(i), "search")
            return True
        except BudgetExceeded:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(12))) == 4
    status = money.summary(store)
    assert status["billed_usd"] == 0.05 and status["pending_reserved_usd"] == 0.4
    with pytest.raises(BudgetExceeded):
        reserve_call(store, "other", "model")


def test_daily_and_notification_delivery_uncertainty_never_retries(store):
    class Telegram:
        def links(self, *args):
            return []

    automation.save(store, automation.AutomationSettings(telegram_alerts=True))
    write_secrets(store.root, {"TG_BOT_TOKEN": "fake", "TG_GROUP_ID": "fake"})
    automation.notice(store, "problem", "Учебный сбой")
    calls = []

    def sender(*args):
        calls.append(1)
        raise ValueError("unknown")

    worker = automation.DailyWorker(store, Telegram(), sender)
    worker.tick()
    worker.tick()
    assert len(calls) == 1 and automation.status(store)["notifications"][0]["status"] == "uncertain"


def test_email_outbox_does_not_retry_unknown_delivery(store):
    write_secrets(
        store.root,
        {
            k: "fixture"
            for k in ["SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM", "SMTP_TO"]
        },
    )
    external_ops.save(store, external_ops.ExternalSettings(email_digest=True))
    calls = []

    def fail(*args):
        calls.append(1)
        raise ValueError("unknown")

    for _ in range(2):
        assert external_ops.deliver_email(store, "week", "text", fail)["status"] == "uncertain"
    assert len(calls) == 1


def test_s3_backup_is_encrypted_restorable_without_secrets_and_cached(store):
    key = Fernet.generate_key().decode()
    write_secrets(
        store.root,
        {
            "BACKUP_REGION": "eu-central-1",
            "BACKUP_BUCKET": "test-bucket",
            "BACKUP_ACCESS_KEY": "fake",
            "BACKUP_SECRET_KEY": "fake",
            "BACKUP_ENCRYPTION_KEY": key,
            "OPENROUTER_API_KEY": "do-not-export-this",
        },
    )
    captured = []

    class S3:
        def put_object(self, **kwargs):
            captured.append(kwargs)

        def close(self):
            pass

    one = external_ops.upload_backup(store, lambda cfg: S3())
    two = external_ops.upload_backup(store, lambda cfg: S3())
    assert one["object_key"] == two["object_key"] and two["cached"] and len(captured) == 1
    plain = Fernet(key.encode()).decrypt(captured[0]["Body"])
    with tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz") as archive:
        assert "workspace.sqlite3" in archive.getnames() and ".env" not in archive.getnames()
        assert all(
            "do-not-export-this" not in archive.extractfile(m).read().decode(errors="ignore")
            for m in archive.getmembers()
        )
    assert captured[0]["ServerSideEncryption"] == "AES256"


def test_task_end_to_end_keyless_error_visible_and_no_secret_echo(store):
    with TestClient(create_app(store.root), headers={"X-Radar-Local": "1"}) as c:
        r = c.post("/api/records/demo-1/tasks", json={"kind": "author"})
        assert r.status_code == 202
        import time

        for _ in range(100):
            result = c.get("/api/tasks/" + r.json()["id"]).json()
            if result["status"] not in {"queued", "running"}:
                break
            time.sleep(0.02)
        assert result["status"] == "failed" and "демо" in result["error"].lower()
        assert c.get("/api/records/demo-1/intelligence").status_code == 200
        assert c.get("/api/external-operations").status_code == 200
        assert c.get("/api/budget").status_code == 200


def test_changed_selected_case_invalidates_client_approval(store):
    real(store)
    payload = payload_for(store, "demo-1")
    case = cases.Case(
        title="Подтверждённый учебный кейс",
        situation="сроки",
        topics=["разные_ответы"],
        description="Описание учебной работы для проверки актуальности.",
        result="Подтверждённый учебный результат.",
        evidence="Фикстура, не реальный опыт.",
        approved=True,
        client_use=True,
    )
    saved = cases.save(store, case)
    import hashlib

    payload["author"] = {
        "base_fingerprint": hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    }
    payload["cases"] = [saved]
    payload["report"]["products"][0]["case_ids"] = [saved["id"]]
    brief = store.save_brief("demo-1", payload)
    assert client_pdf.current(store, review.get_brief(store, brief["id"]))
    cases.save(store, case.model_copy(update={"result": "Исправленный учебный результат."}))
    assert not client_pdf.current(store, review.get_brief(store, brief["id"]))


def test_nightly_partial_read_is_not_reported_completed(store):
    worker = automation.DailyWorker(store, object())
    worker.step("2026-11-11", "fixture-read", lambda: {"partial": True, "ingest": {"added": 1000}})
    assert automation.status(store)["history"][0]["status"] == "partial"


def test_enabled_email_without_configuration_has_visible_blocked_history(store):
    external_ops.save(store, external_ops.ExternalSettings(email_digest=True))

    def forbidden(*args):
        raise AssertionError("No configured transport may be used")

    assert (
        external_ops.deliver_email(store, "fixture-week", "fixture", sender=forbidden)["status"]
        == "blocked"
    )
    assert external_ops.status(store)["email"][0]["status"] == "blocked"
