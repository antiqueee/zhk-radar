import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from telethon import errors
from telethon.tl.types import Channel, ChatPhotoEmpty, User

from radar.web.app import create_app
from radar.web.chats import aggregates
from radar.web.configuration import configuration, write_secrets
from radar.web.discovery import SearchRequest
from radar.web.jobs import JobRunner
from radar.web.operations import (
    BudgetExceeded,
    OperationsSettings,
    WeeklyScheduler,
    next_run,
    reserve_call,
    save,
    send_digest,
    settings,
    usage,
)
from radar.web.store import Store, now
from radar.web.telegram import TelegramCode, TelegramLink, TelegramReader


@pytest.fixture
def store(tmp_path):
    shutil.copytree(Path("config"), tmp_path / "config")
    return Store(tmp_path)


def test_default_off_and_moscow_schedule(store):
    assert not settings(store)["enabled"]
    cfg = OperationsSettings(enabled=True, weekday=0, hour=9)
    # Monday 08:59 Moscow -> one minute later, persisted UTC.
    assert next_run(cfg, datetime(2026, 11, 9, 5, 59, tzinfo=UTC)) == "2026-11-09T06:00:00+00:00"
    assert next_run(cfg, datetime(2026, 11, 9, 6, tzinfo=UTC)) == "2026-11-16T06:00:00+00:00"
    first = save(store, cfg)
    assert save(store, cfg)["next_run"] == first["next_run"]
    assert save(store, OperationsSettings())["next_run"] is None


def test_call_caps_atomic_across_parallel_jobs(store):
    save(store, OperationsSettings(per_run=3, daily=5, monthly=5))

    def attempt(i):
        try:
            reserve_call(store, str(i), "search")
            return True
        except BudgetExceeded:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(20))) == 5
    assert usage(store)["calls"] == 5
    assert usage(store)["cost_usd"] is None


def test_per_run_cap(store):
    save(store, OperationsSettings(per_run=1))
    reserve_call(store, "job", "model")
    with pytest.raises(BudgetExceeded):
        reserve_call(store, "job", "search")
    reserve_call(store, "another-job", "search")


def test_schedule_key_survives_completion(store):
    jobs = JobRunner(store)
    first = jobs.submit(SearchRequest(region="moscow"), "demo", "weekly:a")
    jobs.execute({**first, "request": json.dumps(first["request"])})
    assert jobs.submit(SearchRequest(region="moscow"), "demo", "weekly:a")["id"] == first["id"]
    assert jobs.latest()["status"] == "completed"


class FakeJobs:
    def __init__(self):
        self.calls = []

    def submit(self, request, idempotency_key=None):
        self.calls.append(idempotency_key)
        return {"id": idempotency_key}

    def get(self, ident):
        return {"status": "completed", "result": {"found": 1, "signals_added": 1, "record_ids": []}}


@pytest.mark.parametrize("fail", [False, True])
def test_weekly_delivery_never_repeats_on_uncertain_response(store, fail):
    write_secrets(store.root, {"TG_BOT_TOKEN": "test-secret", "TG_GROUP_ID": "123"})
    save(store, OperationsSettings(enabled=True, delivery=True))
    due = datetime.now(UTC) - timedelta(hours=1)
    with store.connect() as db:
        db.execute("UPDATE operation_settings SET next_run=?", (due.isoformat(),))
    sent = []

    def sender(cfg, text):
        sent.append(text)
        if fail:
            raise ValueError("Доставка не подтверждена")

    jobs = FakeJobs()
    scheduler = WeeklyScheduler(store, jobs, SimpleNamespace(links=lambda: []), sender)
    scheduler.tick()
    scheduler.tick()
    assert len(jobs.calls) == len(sent) == 1
    assert scheduler.history()[0]["status"] == ("uncertain" if fail else "sent")


def test_bot_adapter_sanitizes_token_and_preserves_text():
    seen = []

    def transport(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})

    cfg = {"TG_BOT_TOKEN": "very-secret", "TG_GROUP_ID": "123"}
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        assert send_digest(cfg, "<literal> & text", client) == 77
    assert seen == [
        {
            "chat_id": "123",
            "text": "<literal> & text",
            "link_preview_options": {"is_disabled": True},
        }
    ]
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401))) as client:
        with pytest.raises(ValueError) as error:
            send_digest(cfg, "text", client)
    assert "very-secret" not in str(error.value)


def test_public_link_validation():
    assert TelegramLink(url="https://t.me/s/Valid_chat/").url == "https://t.me/valid_chat"
    for link in [
        "https://t.me/+secret",
        "https://t.me/c/123/1",
        "https://evil.org/chat",
        "https://t.me/valid_chat/123",
        "http://t.me/testchat",
        "https://t.me/share",
    ]:
        with pytest.raises(ValueError):
            TelegramLink(url=link)


class FakeTelegram:
    def __init__(self):
        self.session = SimpleNamespace(save=lambda: "secret-session")
        self.entity = Channel(
            id=99,
            title="Открытый чат",
            photo=ChatPhotoEmpty(),
            date=datetime.now(UTC),
            username="valid_chat",
            megagroup=True,
        )
        self.messages = [
            SimpleNamespace(
                id=i,
                sender_id=500,
                date=datetime.now(UTC),
                message="Телефон +7 999 123 45 67, кв. 17",
            )
            for i in range(1, 4)
        ]
        self.two_factor = False
        self.disconnects = 0

    async def connect(self):
        pass

    async def disconnect(self):
        self.disconnects += 1

    async def send_code_request(self, phone):
        return SimpleNamespace(phone_code_hash="secret-hash")

    async def sign_in(self, **kwargs):
        if self.two_factor and not kwargs.get("password"):
            raise errors.SessionPasswordNeededError(request=None)

    async def is_user_authorized(self):
        return True

    async def get_entity(self, username):
        return self.entity

    async def iter_messages(self, entity, **kwargs):
        for message in [m for m in self.messages if m.id > kwargs["min_id"]][: kwargs["limit"]]:
            yield message


def telegram(store):
    write_secrets(store.root, {"TG_API_ID": "12345", "TG_API_HASH": "secret-api"})
    fake = FakeTelegram()
    reader = TelegramReader(store, lambda: "salt", lambda cfg, session: fake)
    return reader, fake


def test_telegram_login_two_factor_and_secret_storage(store):
    reader, fake = telegram(store)
    assert reader.login("+79991234567")["status"] == "code_sent"
    with pytest.raises(ValueError):
        reader.login("+79991234567")
    fake.two_factor = True
    assert reader.confirm(TelegramCode(code="12345"))["status"] == "password_needed"
    assert reader.confirm(TelegramCode(password="secret-password"))["status"] == "authorized"
    assert configuration(store.root)["TG_SESSION"] == "secret-session"
    assert (store.root / ".env").stat().st_mode & 0o777 == 0o600
    assert "secret-session" not in json.dumps(reader.status())
    reader.disconnect()
    assert not reader.status()["session_saved"]


def test_telegram_cursor_dedup_partial_and_private_rejection(store):
    reader, fake = telegram(store)
    write_secrets(store.root, {"TG_SESSION": "secret-session"})
    link = TelegramLink(url="https://t.me/valid_chat")
    reader.add("demo-1", link)
    reader.add("demo-1", link)
    assert len(reader.links("demo-1")) == 1
    first = reader.sync("demo-1", link.url, limit=2)
    assert first["partial"] and first["ingest"]["added"] == 2
    assert reader.sync("demo-1", link.url)["ingest"]["added"] == 1
    assert reader.sync("demo-1", link.url)["ingest"]["added"] == 0
    assert aggregates(store, "demo-1")["messages_30d"] == 3
    with store.connect() as db:
        message = dict(db.execute("SELECT * FROM local_messages LIMIT 1").fetchone())
    assert "999" not in message["text"] and "17" not in message["text"]
    assert message["author_hash"] != "500" and message["topic"] == "не_размечено"
    fake.entity = User(id=2, first_name="Person", username="valid_chat")
    with pytest.raises(ValueError, match="открытым"):
        reader.sync("demo-1", link.url)


def test_new_api_no_keys_and_no_secret_echo(store):
    with TestClient(create_app(store.root), headers={"X-Radar-Local": "1"}) as client:
        assert (
            client.post(
                "/api/records/demo-1/telegram", json={"url": "https://t.me/valid_chat"}
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/records/demo-1/telegram/sync", json={"url": "https://t.me/valid_chat"}
            ).status_code
            == 400
        )
        assert client.post("/api/telegram/login", json={"phone": "bad-secret"}).status_code == 422
        assert (
            "bad-secret"
            not in client.post("/api/telegram/login", json={"phone": "bad-secret"}).text
        )
        assert not client.get("/api/operations").json()["settings"]["enabled"]
        assert (
            client.post("/api/benchmark", json={"queries": ["ЖК Москва жалобы"]}).status_code == 400
        )


def test_chat_model_grounding_dedup_and_source_method(store):
    from radar.web.chat_analysis import Labels, analyze
    from radar.web.chats import ChatImport, ingest_chat
    from radar.web.configuration import SettingsUpdate, save_settings

    save_settings(
        store.root, SettingsUpdate(values={"OPENROUTER_API_KEY": "fake"}, model="test/model")
    )
    ingest_chat(
        store,
        ChatImport(
            record_id="demo-1",
            chat_id="x",
            title="Open",
            access="public",
            messages=[
                {
                    "id": "1",
                    "author_id": "a",
                    "sent_at": now(),
                    "text": "В корпусе снова протечки, очень плохо.",
                }
            ],
        ),
        "salt",
    )
    called = []

    def classify(client, cfg, model, messages, topics):
        called.append(messages)
        return Labels.model_validate(
            {
                "messages": [
                    {"index": 0, "topic": "протечки", "sentiment": -1, "quote": "снова протечки"}
                ]
            }
        )

    assert analyze(store, "demo-1", classifier=classify)["labelled"] == 1
    assert analyze(store, "demo-1", classifier=classify)["labelled"] == 0
    assert len(called) == 1 and usage(store)["calls"] == 1
    stats = aggregates(store, "demo-1")
    assert stats["model_labelled"] == 1 and stats["negative_share"] == 100
    assert "Моделью размечено 1" in stats["method"]


def test_invalid_chat_classification_never_changes_messages_or_recharges(store):
    from radar.web.chat_analysis import Labels, analyze
    from radar.web.chats import ChatImport, ingest_chat
    from radar.web.configuration import SettingsUpdate, save_settings

    save_settings(
        store.root, SettingsUpdate(values={"OPENROUTER_API_KEY": "fake"}, model="test/model")
    )
    ingest_chat(
        store,
        ChatImport(
            record_id="demo-1",
            chat_id="x",
            title="Open",
            access="public",
            messages=[{"id": "1", "author_id": "a", "sent_at": now(), "text": "Вопрос по ключам"}],
        ),
        "salt",
    )

    def bad(*args):
        return Labels.model_validate(
            {
                "messages": [
                    {"index": 0, "topic": "протечки", "sentiment": -1, "quote": "выдуманная цитата"}
                ]
            }
        )

    for _ in range(2):
        with pytest.raises(ValueError, match="цитатой"):
            analyze(store, "demo-1", classifier=bad)
    assert usage(store)["calls"] == 1
    assert aggregates(store, "demo-1")["topics"] == [("не_размечено", 1)]
