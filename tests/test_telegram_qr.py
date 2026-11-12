import asyncio
import json
import shutil
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from telethon import errors
from telethon.tl.types.auth import CodeTypeSms, SentCodeTypeApp, SentCodeTypeSms

from radar.web.app import create_app
from radar.web.configuration import configuration, write_secrets
from radar.web.store import Store
from radar.web.telegram import TelegramLogin, TelegramReader
from radar.web.telegram_qr import TelegramQR


class FakeClient:
    def __init__(self, behavior="wait"):
        self.behavior = behavior
        self.session = SimpleNamespace(save=lambda: "private-session")
        self.updates = False
        self.waiting = False
        self.generations = 0
        self.disconnected = 0
        self.calls = []

    async def connect(self):
        pass

    async def disconnect(self):
        self.disconnected += 1

    async def set_receive_updates(self, enabled):
        self.updates = enabled

    async def qr_login(self):
        assert self.updates
        self.generations += 1
        return SimpleNamespace(
            url=f"tg://login?token=fake-token-{self.generations}",
            expires=datetime.now(UTC) + timedelta(seconds=30),
            wait=self.wait,
        )

    async def wait(self, timeout):
        self.waiting = True
        if self.behavior == "refresh" and self.generations == 1:
            raise TimeoutError()
        if self.behavior == "password":
            raise errors.SessionPasswordNeededError(None)
        if self.behavior == "success":
            return
        await asyncio.Event().wait()

    async def is_user_authorized(self):
        return True

    async def sign_in(self, password):
        if password != "correct":
            raise errors.PasswordHashInvalidError(None)

    async def send_code_request(self, phone):
        return SimpleNamespace(
            phone_code_hash="private-hash",
            type=SentCodeTypeApp(5),
            next_type=CodeTypeSms(),
            timeout=30,
        )

    async def __call__(self, request):
        self.calls.append(request)
        return SimpleNamespace(
            phone_code_hash="new-hash",
            type=SentCodeTypeSms(5),
            next_type=None,
            timeout=None,
        )


@pytest.fixture
def reader(tmp_path):
    shutil.copytree("config", tmp_path / "config")
    store = Store(tmp_path)
    write_secrets(tmp_path, {"TG_API_ID": "12345", "TG_API_HASH": "private-api"})
    fake = FakeClient()

    def factory(cfg, session):
        fake.updates = cfg.get("_receive_updates", False)
        return fake

    reader = TelegramReader(store, lambda: "salt", factory)
    reader.qr = TelegramQR(reader)
    return reader, fake


async def wait_status(qr, expected):
    for _ in range(200):
        if qr.status()["status"] == expected:
            return qr.status()
        await asyncio.sleep(0.01)
    raise AssertionError(qr.status()["status"])


def test_qr_wait_refresh_idempotent_cancel(reader):
    reader, fake = reader
    fake.behavior = "refresh"

    async def scenario():
        await reader.qr.start()
        await wait_status(reader.qr, "waiting")
        for _ in range(100):
            if fake.generations == 2:
                break
            await asyncio.sleep(0.01)
        assert fake.generations == 2
        assert fake.waiting and fake.updates
        state = await reader.qr.start()
        assert state["image"].startswith("data:image/svg+xml;base64,")
        assert fake.generations == 2
        with pytest.raises(ValueError, match="QR"):
            reader.login("+79991234567")
        await reader.qr.cancel()
        assert fake.disconnected == 1
        assert "image" not in reader.qr.status()
        assert not configuration(reader.store.root).get("TG_SESSION")

    asyncio.run(scenario())


def test_qr_success_saves_only_secret_session(reader):
    reader, fake = reader
    fake.behavior = "success"

    async def scenario():
        await reader.qr.start()
        state = await wait_status(reader.qr, "authorized")
        assert "image" not in state
        assert "private" not in json.dumps(state)
        assert configuration(reader.store.root)["TG_SESSION"] == "private-session"
        await reader.qr.cancel()
        assert configuration(reader.store.root)["TG_SESSION"] == "private-session"
        assert (reader.store.root / ".env").stat().st_mode & 0o777 == 0o600

    asyncio.run(scenario())


def test_qr_two_factor_retry(reader):
    reader, fake = reader
    fake.behavior = "password"

    async def scenario():
        await reader.qr.start()
        state = await wait_status(reader.qr, "password_needed")
        assert "image" not in state
        assert not configuration(reader.store.root).get("TG_SESSION")
        with pytest.raises(ValueError, match="Неверный пароль"):
            await reader.qr.confirm("wrong")
        assert reader.qr.status()["status"] == "password_needed"
        assert (await reader.qr.confirm("correct"))["status"] == "authorized"
        assert reader.qr.password_session is None
        await reader.qr.cancel()

    asyncio.run(scenario())


def test_qr_expired_password_cannot_authorize(reader):
    reader, fake = reader
    fake.behavior = "password"

    async def scenario():
        await reader.qr.start()
        await wait_status(reader.qr, "password_needed")
        reader.qr.deadline = 0
        with pytest.raises(ValueError, match="QR"):
            await reader.qr.confirm("correct")
        assert reader.qr.password_session is None
        assert not configuration(reader.store.root).get("TG_SESSION")
        await reader.qr.cancel()

    asyncio.run(scenario())


def test_code_delivery_resend_and_secrets(reader):
    reader, fake = reader
    state = reader.login("+79991234567")
    assert state["delivery"] == "SentCodeTypeApp"
    assert "не SMS" in state["message"]
    assert state["resend_available"]
    with pytest.raises(ValueError, match="через"):
        reader.resend()
    assert not fake.calls
    reader.pending["resend_at"] = 0
    assert reader.resend()["delivery"] == "SentCodeTypeSms"
    assert fake.calls[0].phone_code_hash == "private-hash"
    assert reader.pending["hash"] == "new-hash"
    with pytest.raises(ValueError, match="не предложил"):
        reader.resend()
    assert "private" not in json.dumps(reader.status())
    assert TelegramLogin(phone="+7 (999) 123-45-67").phone == "+79991234567"


def test_qr_http_flow_and_no_cache(reader):
    reader, fake = reader
    app = create_app(reader.store.root)
    app.state.telegram.factory = reader.factory
    with TestClient(app) as client:
        assert client.post("/api/telegram/qr").status_code == 403
        response = client.post("/api/telegram/qr", headers={"X-Radar-Local": "1"})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        for _ in range(200):
            state = client.get("/api/telegram/qr").json()
            if state["status"] == "waiting":
                break
            import time

            time.sleep(0.01)
        assert state["status"] == "waiting"
        assert "private-api" not in json.dumps(state)
        assert client.delete("/api/telegram/qr", headers={"X-Radar-Local": "1"}).status_code == 200
        assert client.get("/api/telegram/qr").json()["status"] == "idle"
