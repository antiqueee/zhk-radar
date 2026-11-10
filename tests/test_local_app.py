import io
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from radar.web.app import create_app
from radar.web.store import Store


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTHOR_HASH_SALT", raising=False)
    shutil.copytree(Path("config"), tmp_path / "config")
    app = create_app(tmp_path)
    with TestClient(app, headers={"X-Radar-Local": "1"}) as client:
        yield client


def decision(client, **changes):
    record = client.get("/api/records/demo-1").json()
    data = {
        key: record[key] for key in ["revision", "status", "note", "stage", "situation", "topics"]
    }
    return {**data, **changes}


def test_local_guard(client):
    assert client.post("/api/run", headers={"X-Radar-Local": "0"}).status_code == 403
    assert client.post("/api/run", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/run", headers={"Content-Length": "invalid"}).status_code == 400
    assert client.post("/api/run", headers={"Content-Length": "13000000"}).status_code == 413
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/").headers["x-content-type-options"] == "nosniff"


def test_decision_persistence_and_stale_write(client):
    assert len(client.get("/api/records?dataset=demo").json()) == 4
    data = decision(client, status="inwork", note="Позвонить после проверки")
    result = client.put("/api/records/demo-1", json=data)
    assert result.status_code == 200
    assert result.json()["revision"] == data["revision"] + 1
    assert client.put("/api/records/demo-1", json=data).status_code == 409
    other = Store(client.app.state.store.root)
    assert other.get("demo-1")["note"] == data["note"]
    assert len(other.get("demo-1")["events"]) == 1
    assert client.get("/api/records/missing").status_code == 404
    assert (
        client.put("/api/records/demo-1", json=decision(client, topics=["unknown"])).status_code
        == 400
    )


def test_quote_validation_and_dedup(client):
    evidence = dict(
        title="Документ",
        url="https://example.org/source",
        quote="Срок изменён",
        source_text="Официальный текст. Срок изменён.",
    )
    assert (
        client.post(
            "/api/records/demo-1/evidence", json={**evidence, "quote": "выдумка"}
        ).status_code
        == 400
    )
    for _ in range(2):
        result = client.post("/api/records/demo-1/evidence", json=evidence)
        assert result.status_code == 200
        assert len(result.json()["evidence"]) == 1


def test_pdf_versions_and_rejected_record(client):
    first = client.post("/api/records/demo-1/brief")
    assert first.status_code == 200, first.text
    again = client.post("/api/records/demo-1/brief").json()
    assert first.json()["id"] == again["id"]
    pdf = client.get(f"/api/briefs/{again['id']}/pdf")
    reader = PdfReader(io.BytesIO(pdf.content))
    assert len(reader.pages) == 2
    assert "Северный берег" in reader.pages[0].extract_text()
    assert "Пилот на 30 дней" in reader.pages[1].extract_text()
    client.put("/api/records/demo-1", json=decision(client, note="Новая версия"))
    assert client.post("/api/records/demo-1/brief").json()["version"] == 2
    client.put("/api/records/demo-1", json=decision(client, status="rejected"))
    assert client.post("/api/records/demo-1/brief").status_code == 400
    assert client.get("/api/briefs/9999/pdf").status_code == 404


def chat_batch(**changes):
    return {
        "record_id": "demo-1",
        "chat_id": "public-chat",
        "title": "Открытый чат",
        "access": "public",
        "messages": [
            dict(
                id="123",
                author_id="private-author-456",
                author_name="Иван Петров",
                sent_at=datetime.now(UTC).isoformat(),
                text="Иван Петров: +7 (999) 123-45-67, кв. 99, @private_nick, x@example.org",
                topic="разные_ответы",
                sentiment=-1,
            )
        ],
        **changes,
    }


def test_chat_dedup_redaction_and_aggregate(client):
    batch = chat_batch()
    assert client.post("/api/chats/import", json=batch).json()["added"] == 1
    assert client.post("/api/chats/import", json=batch).json()["added"] == 0
    stats = client.get("/api/chats?dataset=demo").json()[0]
    assert stats["messages_30d"] == stats["authors_30d"] == 1
    assert stats["negative_share"] == 100
    assert "private-author" not in json.dumps(stats)
    with client.app.state.store.connect() as db:
        row = dict(db.execute("SELECT * FROM local_messages").fetchone())
    assert len(row["author_hash"]) == 64
    for private in ["Иван", "999", "private_nick", "x@example.org", "private-author"]:
        assert private not in json.dumps(row, ensure_ascii=False)
    salt = client.app.state.store.root / ".env"
    assert salt.stat().st_mode & 0o777 == 0o600


def test_chat_access_dates_and_missing_labels(client):
    batch = chat_batch(access="private")
    assert client.post("/api/chats/import", json=batch).status_code == 422
    batch = chat_batch()
    batch["messages"][0]["sent_at"] = "2026-01-01T00:00:00"
    assert client.post("/api/chats/import", json=batch).status_code == 422
    batch["messages"][0]["sent_at"] = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    assert client.post("/api/chats/import", json=batch).status_code == 400
    batch["messages"][0]["sent_at"] = datetime.now(UTC).isoformat()
    batch["messages"][0].pop("sentiment")
    assert client.post("/api/chats/import", json=batch).status_code == 200
    assert client.get("/api/chats?dataset=demo").json()[0]["negative_share"] is None


def test_retention_and_no_fake_monitoring(client):
    batch = chat_batch()
    batch["messages"][0]["sent_at"] = (datetime.now(UTC) - timedelta(days=190)).isoformat()
    assert client.post("/api/chats/import", json=batch).status_code == 200
    with client.app.state.store.connect() as db:
        assert db.execute("SELECT text FROM local_messages").fetchone()[0] == ""
    assert client.get("/api/chats?dataset=demo").json()[0]["messages_30d"] == 0
    run = client.post("/api/run").json()
    assert run["external_requests"] == run["cost_usd"] == 0
    assert run["status"] == "partial"
    assert all(not s["connected"] for s in client.get("/api/settings").json()["services"])


def test_import_bad_files(client):
    assert client.post("/api/import", files={"file": ("bad.txt", b"123")}).status_code == 400
    assert client.post("/api/import", files={"file": ("bad.xlsx", b"123")}).status_code == 400


def test_settings_save_and_no_secret_echo(client):
    secret = "private-api-key-xyz"
    payload = {
        "values": {
            "YANDEX_SEARCH_API_KEY": secret,
            "YANDEX_FOLDER_ID": "folder",
            "OPENROUTER_API_KEY": "private-openrouter",
        },
        "model": "test/extractor",
    }
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 200
    assert secret not in response.text
    result = client.get("/api/settings")
    assert secret not in result.text and "private-openrouter" not in result.text
    assert result.json()["fields"]["YANDEX_SEARCH_API_KEY"]
    assert result.json()["search_ready"]
    assert result.json()["model"] == "test/extractor"
    client.put("/api/settings", json={"values": {}, "model": "test/extractor"})
    assert secret in (client.app.state.store.root / ".env").read_text()
    bad = client.put("/api/settings", json={"values": {"UNKNOWN": secret}})
    assert bad.status_code == 422 and secret not in bad.text
    assert (client.app.state.store.root / ".env").stat().st_mode & 0o777 == 0o600


def test_search_without_configuration_has_no_side_effect(client):
    assert client.post("/api/search", json={"region": "capitals"}).status_code == 400
    assert client.get("/api/records?dataset=web").json() == []


def test_chat_import_updates_pdf_version_and_topics(client):
    before = client.post("/api/records/demo-1/brief").json()
    assert client.post("/api/chats/demo").status_code == 200
    after = client.post("/api/records/demo-1/brief").json()
    assert after["version"] == before["version"] + 1
    assert after["payload"]["chat_stats"]["messages_30d"] == 42
    pdf = client.get(f"/api/briefs/{after['id']}/pdf")
    text = PdfReader(io.BytesIO(pdf.content)).pages[0].extract_text()
    assert "42 сообщений" in text
    assert "9 авторов" in text
    assert client.post("/api/records/demo-1/brief").json()["id"] == after["id"]
