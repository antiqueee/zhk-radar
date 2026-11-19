import io
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter

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
    assert "Пилот на 30 дней" in "".join(page.extract_text() for page in reader.pages)
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
    assert "999" not in row["text"]  # Hashes/timestamps may coincidentally contain these digits.
    for private in [
        "Иван",
        "+7 (999) 123-45-67",
        "private_nick",
        "x@example.org",
        "private-author",
    ]:
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
    text = "".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf.content)).pages)
    assert "42 сообщений" in text
    assert "9 авторов" in text
    assert client.post("/api/records/demo-1/brief").json()["id"] == after["id"]


def test_download_latest_does_not_submit_generation_or_mutate_record(client):
    assert client.get("/api/records/demo-1/brief/latest").json() is None
    saved = client.post("/api/records/demo-1/brief").json()
    before = client.get("/api/records/demo-1").json()["revision"]
    for _ in range(2):
        latest = client.get("/api/records/demo-1/brief/latest").json()
        assert latest["id"] == saved["id"]
        response = client.get(f"/api/briefs/{latest['id']}/pdf")
        assert response.status_code == 200 and response.content.startswith(b"%PDF")
    assert client.get("/api/records/demo-1").json()["revision"] == before
    with client.app.state.store.connect() as db:
        assert db.execute("SELECT count(*) FROM object_tasks").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM briefs WHERE record_id='demo-1'").fetchone()[0] == 1


def test_latest_download_respects_enabled_report_format(client):
    store = client.app.state.store
    (store.root / ".env").write_text("RADAR_REPORT_KIT=true\n")
    with store.connect() as db:
        db.execute("UPDATE records SET dataset='web' WHERE id='demo-1'")
    legacy = store.save_brief("demo-1", {"name": "Test", "report": {}})
    assert client.get("/api/records/demo-1/brief/latest").json() is None
    assert client.get("/api/records?dataset=web").json()[0]["report_status"]["code"] == "none"
    modern = store.save_brief(
        "demo-1",
        {
            "report_v2": {"meta": {"layout": "full", "schema_version": "5"}},
            "report_pipeline": {"status": "passed", "passed": True},
        },
    )
    later_legacy = store.save_brief("demo-1", {"name": "Test old", "report": {}})
    assert later_legacy["id"] > modern["id"] > legacy["id"]
    assert client.get("/api/records/demo-1/brief/latest").json()["id"] == modern["id"]
    latest = client.get("/api/records/demo-1/brief/latest").json()
    assert latest["format_version"] == "5"
    assert latest["current_format"] is False
    assert latest["report_status"]["code"] == "outdated"
    web_records = client.get("/api/records?dataset=web").json()
    assert web_records[0]["report_status"] == latest["report_status"]
    assert client.get("/api/records/demo-1").json()["report_status"] == latest["report_status"]
    (store.root / ".env").write_text("RADAR_REPORT_KIT=false\n")
    latest = client.get("/api/records/demo-1/brief/latest").json()
    assert latest["id"] == later_legacy["id"]
    assert latest["report_status"]["code"] == "ready"


def test_latest_report_identifies_v8_without_returning_full_report(client):
    store = client.app.state.store
    (store.root / ".env").write_text("RADAR_REPORT_KIT=true\n")
    with store.connect() as db:
        db.execute("UPDATE records SET dataset='web' WHERE id='demo-1'")
    current = store.save_brief("demo-1", {
        "report_v2": {"meta": {"schema_version": "8"}},
        "report_pipeline": {"version": "8", "status": "passed", "passed": True},
    })
    latest = client.get("/api/records/demo-1/brief/latest").json()
    assert latest["id"] == current["id"]
    assert latest["format_version"] == "8"
    assert latest["current_format"] is True
    assert latest["downloadable"] is True
    assert "report_v2" not in latest["payload"]


def test_latest_v8_qualification_is_current_even_without_a_pdf(client):
    store = client.app.state.store
    (store.root / ".env").write_text("RADAR_REPORT_KIT=true\n")
    with store.connect() as db:
        db.execute("UPDATE records SET dataset='web' WHERE id='demo-1'")
    store.save_brief("demo-1", {
        "report_qualification": {"verdict": "watch", "reasons": ["below_threshold"]},
        "report_pipeline": {"version": "report-kit-v8", "status": "watch", "passed": False},
    })
    latest = client.get("/api/records/demo-1/brief/latest").json()
    assert latest["format_version"] == "8"
    assert latest["current_format"] is True
    assert latest["downloadable"] is False
    assert latest["report_status"]["code"] == "watch"
    web_records = client.get("/api/records?dataset=web").json()
    assert web_records[0]["report_status"] == latest["report_status"]


def test_latest_v8_draft_exposes_a_separate_earlier_approved_version(client):
    store = client.app.state.store
    (store.root / ".env").write_text("RADAR_REPORT_KIT=true\n")
    with store.connect() as db:
        db.execute("UPDATE records SET dataset='web' WHERE id='demo-1'")
    approved = store.save_brief("demo-1", {
        "report_v2": {"meta": {"schema_version": "8"}},
        "report_pipeline": {"status": "passed", "passed": True},
    })
    store.save_brief("demo-1", {
        "report_v2": {"meta": {"schema_version": "8"}},
        "report_pipeline": {"status": "manual", "passed": True},
    })
    store.save_brief("demo-1", {
        "report_v2": {"meta": {"schema_version": "5"}},
        "report_pipeline": {"status": "passed", "passed": True},
    })
    draft = store.save_brief("demo-1", {
        "report_v2": None,
        "report_pipeline": {"version": "report-kit-v8", "status": "manual", "passed": False},
    })

    latest = client.get("/api/records/demo-1/brief/latest").json()
    assert latest["id"] == draft["id"]
    assert latest["format_version"] == "8" and latest["downloadable"] is False
    assert latest["label"] == "Нужна редакторская проверка"
    fallback = latest["previous_approved_v8"]
    assert (fallback["id"], fallback["version"]) == (approved["id"], approved["version"])
    assert client.get(f"/api/briefs/{draft['id']}/pdf").status_code == 409


def test_download_v8_reuses_valid_cache_and_recovers_corrupt_cache(client, monkeypatch):
    from radar.reports import render
    from radar.reports.schema_v8 import ReportV8

    store = client.app.state.store
    (store.root / ".env").write_text("RADAR_REPORT_KIT=true\n")
    with store.connect() as db:
        db.execute("UPDATE records SET dataset='web' WHERE id='demo-1'")
    fixture = render.ROOT / "gold/v8/nametkin-tower.json"
    report = ReportV8.model_validate_json(fixture.read_bytes()).model_dump()
    approved = store.save_brief("demo-1", {
        "dataset": "web",
        "report_v2": report,
        "report_pipeline": {"status": "passed", "passed": True},
        "report_qualification": {"verdict": "report_full"},
    })
    brief_id = approved["id"]
    cache = store.root / "data/local/report-pdfs" / f"{brief_id}.pdf"
    writer = PdfWriter()
    for _ in range(6):
        writer.add_blank_page(width=595, height=842)
    document = io.BytesIO()
    writer.write(document)
    pdf = document.getvalue()
    render.write_pdf_cache(cache, pdf)
    calls = []

    def rerender(data):
        calls.append(data)
        return pdf

    monkeypatch.setattr(render, "pdf_bytes", rerender)
    latest = client.get("/api/records/demo-1/brief/latest").json()
    assert (latest["id"], latest["format_version"], latest["downloadable"]) == (brief_id, "8", True)
    url = f"/api/briefs/{brief_id}/pdf"
    assert client.get(url).content == pdf
    assert not calls

    for damaged in (b"", pdf[:-20]):
        cache.write_bytes(damaged)
        response = client.get(url)
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert len(PdfReader(io.BytesIO(response.content), strict=True).pages) == 6
        assert cache.read_bytes() == response.content
        assert calls[-1]["meta"]["schema_version"] == "8"
    assert len(calls) == 2
    assert client.get(url).content == pdf
    assert len(calls) == 2

    cache.write_bytes(b"")
    preview = client.get(f"/api/briefs/{brief_id}/client-preview")
    assert preview.status_code == 200 and preview.content == pdf
    assert cache.read_bytes() == pdf and len(calls) == 3

    cache.write_bytes(b"")
    monkeypatch.setattr(render, "pdf_bytes", lambda _: b"incomplete")
    response = client.get(url)
    assert response.status_code == 503
    assert response.headers["content-type"] == "application/json"
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM object_tasks").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM briefs").fetchone()[0] == 1


def test_record_exposes_extended_search_metadata_without_raw_documents(client):
    from radar.web.report_search import initialize

    store = client.app.state.store
    initialize(store)
    document = {
        "source_id": "source-public", "url": "https://example.org/article",
        "title": "Материал об объекте", "published_at": "2026-10-31",
        "read_status": "loaded", "source_kind": "page", "contour": "media",
        "text": "RAW_PAGE_MUST_NOT_SHIP", "messages": [{"text": "RAW_CHAT_MUST_NOT_SHIP"}],
        "message_urls": ["https://t.me/private/secret"],
    }
    research = {
        "version": "object-research-v5", "created_at": "2026-11-16T12:00:00+00:00",
        "completed": False, "documents": [document],
        "queries": ["INTERNAL_QUERY_MUST_NOT_SHIP"],
    }
    with store.connect() as db:
        db.execute(
            "INSERT INTO report_research VALUES(?,?,?)",
            ("demo-1", json.dumps(research), research["created_at"]),
        )
    response = client.get("/api/records/demo-1")
    metadata = response.json()["report_research"]
    assert set(metadata) == {"version", "created_at", "completed", "documents"}
    assert metadata["completed"] is False
    assert metadata["documents"] == [{
        key: document[key] for key in (
            "source_id", "url", "title", "published_at", "read_status", "source_kind", "contour"
        )
    }]
    assert "MUST_NOT_SHIP" not in response.text
    assert "private/secret" not in response.text
    assert client.get("/api/records/demo-2").json()["report_research"] is None


def test_record_chat_counts_survive_without_an_automatic_screening(client):
    before = client.get("/api/records/demo-1").json()["chat_stats"]
    assert before["messages_30d"] == 0 and before["chats"] == []
    client.post("/api/chats/demo")
    record = client.get("/api/records/demo-1").json()
    assert record["chat_stats"]["messages_30d"] == 42
    assert len(record["chat_stats"]["chats"]) == 1
    assert record["chat_stats"]["labelled"] == 42
    assert client.get("/api/records/demo-1/intelligence").json()["screening"] is None


@pytest.mark.parametrize('status', ['hold', 'watch', 'manual'])
def test_unqualified_or_unapproved_report_is_never_downloaded_even_if_cached(client, status):
    store = client.app.state.store
    payload = {
        'name': 'Тест',
        'report_pipeline': {'status': status, 'passed': False},
        'report_qualification': {
            'verdict': status if status != 'manual' else 'report_full',
            'reasons': ['below_threshold'] if status == 'watch' else [],
            'manual_checks': ['Проверить текущий статус'],
        },
    }
    if status == 'manual':
        payload['report_v2'] = {'meta': {'schema_version': '5'}}
    brief = store.save_brief('demo-1', payload)
    cache = store.root / 'data/local/report-pdfs'
    cache.mkdir(parents=True)
    (cache / f"{brief['id']}.pdf").write_bytes(b'%PDF-old-unapproved')
    response = client.get(f"/api/briefs/{brief['id']}/pdf")
    assert response.status_code == 409
    assert not response.content.startswith(b'%PDF')
    history = client.get('/api/records/demo-1/brief-history').json()
    assert history[0]['downloadable'] is False
    assert client.get(f"/api/briefs/{brief['id']}/client-preview").status_code == 400


def test_latest_qualification_supersedes_previously_passed_pdf(client):
    store = client.app.state.store
    (store.root / '.env').write_text('RADAR_REPORT_KIT=true\n')
    with store.connect() as db:
        db.execute("UPDATE records SET dataset='web' WHERE id='demo-1'")
    store.save_brief('demo-1', {
        'report_v2': {'meta': {'schema_version': '5'}},
        'report_pipeline': {'status': 'passed', 'passed': True},
    })
    hold = store.save_brief('demo-1', {
        'report_qualification': {'verdict': 'hold', 'reasons': ['developer_unknown']},
        'report_pipeline': {'status': 'hold', 'passed': False},
    })
    latest = client.get('/api/records/demo-1/brief/latest').json()
    assert latest['id'] == hold['id']
    assert latest['downloadable'] is False
    assert 'Не установлен застройщик' in latest['detail']
