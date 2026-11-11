import io
import json
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from radar.web import maintenance, registry, review, sheets, stoplist
from radar.web.app import create_app
from radar.web.auth import COOKIE, Auth, Login, server_config, set_user
from radar.web.configuration import SettingsUpdate, save_settings, write_secrets
from radar.web.operations import settings, usage
from radar.web.store import Store
from radar.web.weekly_steps import complete_steps


@pytest.fixture
def store(tmp_path):
    shutil.copytree(Path("config"), tmp_path / "config")
    return Store(tmp_path)


def snapshot(**changes):
    return registry.RegistrySnapshot(
        **{
            "house_id": "12345",
            "name": "Учебный корпус",
            "observed_at": datetime.now(UTC) - timedelta(days=1),
            "source_url": "https://наш.дом.рф/12345",
            **changes,
        }
    )


def make_real(store):
    # Synthetic fixture explicitly changed to exercise non-demo paths; never live data.
    with store.connect() as db:
        db.execute("UPDATE records SET dataset='web' WHERE id='demo-1'")


def test_auth_fail_closed_roles_origin_and_revocation(store):
    write_secrets(store.root, {"RADAR_SERVER_MODE": "true"})
    with pytest.raises(ValueError, match="PUBLIC_ORIGIN"):
        server_config(store.root)
    write_secrets(store.root, {"RADAR_PUBLIC_ORIGIN": "https://radar.example.org"})
    with pytest.raises(ValueError, match="администратора"):
        server_config(store.root)
    for name, role in [("admin", "admin"), ("analyst", "analyst"), ("viewer", "viewer")]:
        set_user(store.root, name, "long-passphrase-123", role)
    app = create_app(store.root)
    c = TestClient(
        app,
        base_url="https://radar.example.org",
        headers={"X-Radar-Local": "1", "Origin": "https://radar.example.org"},
    )
    assert c.get("/api/records").status_code == 401
    assert "login-form" in c.get("/").text
    assert c.get("/healthz").status_code == 200
    assert (
        c.post("/api/auth/login", json={"username": "viewer", "password": "wrong"}).status_code
        == 400
    )
    r = c.post("/api/auth/login", json={"username": "viewer", "password": "long-passphrase-123"})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
    assert c.get("/api/records?dataset=demo").status_code == 200
    assert c.get("/api/settings").status_code == 403
    assert c.post("/api/records/demo-1/brief").status_code == 403
    assert c.post("/api/auth/logout", headers={"Origin": "https://evil.org"}).status_code == 403
    assert c.post("/api/auth/logout").status_code == 200
    assert c.get("/api/session").status_code == 401
    c.post("/api/auth/login", json={"username": "analyst", "password": "long-passphrase-123"})
    assert c.get("/api/search/readiness").status_code == 200
    assert c.post("/api/records/demo-1/brief").status_code == 200
    assert c.post("/api/sheets/sync").status_code == 403
    token = c.cookies.get(COOKIE)
    set_user(store.root, "analyst", "new-long-password-456", "viewer")
    assert Auth(store).session(token) is None
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM access_audit").fetchone()[0] >= 4
        assert "long-passphrase" not in str(list(db.execute("SELECT * FROM access_audit")))


def test_auth_attempt_limit(store):
    set_user(store.root, "admin", "long-passphrase-123", "admin")
    write_secrets(
        store.root,
        {"RADAR_SERVER_MODE": "true", "RADAR_PUBLIC_ORIGIN": "https://radar.example.org"},
    )
    auth = Auth(store)
    for _ in range(10):
        with pytest.raises(ValueError, match="Неверное"):
            auth.login(Login(username="admin", password="bad"), "127.0.0.1")
    with pytest.raises(ValueError, match="Слишком много"):
        auth.login(Login(username="admin", password="long-passphrase-123"), "127.0.0.1")


def test_stoplist_exact_developer_and_pdf_block(store):
    c = TestClient(create_app(store.root), headers={"X-Radar-Local": "1"})
    r = store.get("demo-1")
    entry = stoplist.StopEntry(kind="developer", value=r["developer"], reason="client")
    stoplist.add(store, entry)
    stoplist.add(store, entry)
    assert len(stoplist.entries(store)) == 1
    assert stoplist.blocked(store, r)
    assert not stoplist.blocked(store, {**r, "developer": r["developer"] + " другой"})
    assert c.post("/api/records/demo-1/brief").status_code == 400
    stoplist.remove(store, stoplist.entries(store)[0]["id"])
    assert c.post("/api/records/demo-1/brief").status_code == 200


def test_registry_unknown_rve_stale_manual_trust_and_history(store):
    registry.save_snapshot(
        store,
        snapshot(
            commissioning="2026-01-01",
            readiness=10,
            observed_at=datetime.now(UTC) - timedelta(days=120),
        ),
    )
    last = snapshot(
        commissioning="2026-05-01", readiness=20, observed_at=datetime.now(UTC) - timedelta(days=40)
    )
    assert registry.save_snapshot(store, last)["added"]
    assert not registry.save_snapshot(store, last)["added"]
    registry.bind(store, "demo-1", registry.RegistryBinding(house_id="12345"))
    assert registry.dossier(store, "demo-1")["assessment"] is None
    reg = registry.bind(store, "demo-1", registry.RegistryBinding(house_id="12345", confirmed=True))
    assert len(reg["changes"]) == 2
    score = reg["assessment"]
    assert not score["eligible"] and score["confidence"] == "low"
    assert "R3" not in score["early_rules"]
    assert any("РВЭ не проверено" in s for s in score["limitations"])
    assert any("30 дней" in s for s in score["limitations"])


def test_registry_adapter_daily_cache_and_wrong_identity(store):
    calls = []

    def transport(url):
        calls.append(url)
        return (
            200,
            {},
            json.dumps(
                {
                    "data": {
                        "objId": 12345,
                        "objCommercNm": "Учебный",
                        "objReady100PercDt": "2026-12-31",
                    }
                }
            ).encode(),
        )

    registry.fetch_house(store, "12345", transport)
    assert registry.fetch_house(store, "12345", transport)["cached"]
    assert len(calls) == 1
    with pytest.raises(ValueError, match="другую карточку"):
        registry.parse_dom({"data": {"objId": 1}}, "12345")
    with pytest.raises(ValueError, match="HTTP 403"):
        registry.fetch_house(store, "98765", lambda url: (403, {}, b"forbidden"))


def test_registry_import_cannot_claim_live_and_pdf_contains_snapshot(store):
    c = TestClient(create_app(store.root), headers={"X-Radar-Local": "1"})
    r = c.post("/api/registry/import", json=snapshot(imported=False).model_dump(mode="json"))
    assert r.json()["snapshot"]["imported"] is True
    c.put("/api/records/demo-1/registry", json={"house_id": "12345", "confirmed": True})
    brief = c.post("/api/records/demo-1/brief")
    assert brief.status_code == 200, brief.text
    pdf = c.get(f"/api/briefs/{brief.json()['id']}/pdf")
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf.content)).pages)
    assert "РЕЕСТР КОРПУСА" in text and "Наличие не проверено" in text


def test_erz_parser_does_not_read_mobile_labels_as_figures():
    html = """<app-developer-item>
    <div class="developer-td-3"><a href="/zastroyschiki/x">ГК X</a></div>
    <div class="developer-td-4"><span class="developer-td__title">Строится 99</span>1 000</div>
    <div class="developer-td-5">120</div><div class="developer-td-6">12,0%</div>
    <div class="developer-td-7">3,5</div></app-developer-item>"""
    row = registry.parse_erz(html)[0]
    assert row["building_area"] == 1000 and row["delayed_share_pct"] == 12
    assert row["shift_months"] == 3.5
    with pytest.raises(ValueError):
        registry.parse_erz("<h1>Новая неизвестная структура</h1>")


def test_backup_verified_private_idempotent_automatic_and_recoverable(store):
    result = maintenance.backup(store, automatic=True)
    path = store.root / "data/backups" / result["file"]
    assert path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT count(*) FROM records").fetchone()[0] >= 4
    assert maintenance.backup(store, automatic=True)["file"] == result["file"]
    assert len(list(path.parent.glob("*.sqlite3"))) == 1


def test_semantic_review_cache_grounding_and_version_bound_approval(store):
    make_real(store)
    c = TestClient(create_app(store.root), headers={"X-Radar-Local": "1"})
    c.post(
        "/api/records/demo-1/evidence",
        json={
            "title": "Источник",
            "url": "https://example.org/x",
            "quote": "Срок изменён",
            "source_text": "Срок изменён. Требуется уточнение.",
        },
    )
    ident = c.post("/api/records/demo-1/brief").json()["id"]
    save_settings(
        store.root,
        SettingsUpdate(
            values={"OPENROUTER_API_KEY": "dummy"}, model="test/extract", review_model="test/review"
        ),
    )
    calls = []

    def reviewer(client, cfg, model, payload, docs):
        calls.append(docs)
        return review.ReviewResult(verdict="passed", issues=[])

    review.semantic_review(store, ident, reviewer=reviewer, client=object())
    review.semantic_review(store, ident, reviewer=reviewer, client=object())
    assert len(calls) == 1 and usage(store)["calls"] == 1
    review.approve(
        store,
        ident,
        review.OperatorReview(approved=True, note="Проверил корпус и источник"),
        "analyst",
    )
    assert review.operator_approved(store, ident)
    # Any actual content change creates another version with no inherited approval.
    c.post(
        "/api/records/demo-1/evidence",
        json={
            "title": "Другой",
            "url": "https://example.org/y",
            "quote": "Новый факт",
            "source_text": "Новый факт для проверки.",
        },
    )
    new = c.post("/api/records/demo-1/brief").json()["id"]
    assert new != ident and not review.operator_approved(store, new)


def test_invalid_reviewer_reference_is_cached_but_never_accepted(store):
    c = TestClient(create_app(store.root), headers={"X-Radar-Local": "1"})
    c.post(
        "/api/records/demo-1/evidence",
        json={
            "title": "Источник",
            "url": "https://example.org/x",
            "quote": "Срок изменён",
            "source_text": "Срок изменён.",
        },
    )
    ident = c.post("/api/records/demo-1/brief").json()["id"]
    save_settings(
        store.root,
        SettingsUpdate(
            values={"OPENROUTER_API_KEY": "dummy"}, model="test/extract", review_model="test/review"
        ),
    )

    def bad(*args):
        return review.ReviewResult(
            verdict="passed",
            issues=[
                review.Issue(
                    block="source",
                    severity="warning",
                    reason="Неизвестная ссылка",
                    source_ids=["invented"],
                )
            ],
        )

    for _ in range(2):
        with pytest.raises(ValueError, match="несуществующий"):
            review.semantic_review(store, ident, reviewer=bad, client=object())
    assert usage(store)["calls"] == 1 and not review.history(store, ident)


def test_keyless_review_requires_operator_and_demo_cannot_be_approved(store):
    c = TestClient(create_app(store.root), headers={"X-Radar-Local": "1"})
    ident = c.post("/api/records/demo-1/brief").json()["id"]
    result = c.post(f"/api/briefs/{ident}/review").json()
    assert result[0]["payload"]["verdict"] == "manual"
    assert (
        c.put(
            f"/api/briefs/{ident}/approval",
            json={"approved": True, "note": "Проверка учебного примера"},
        ).status_code
        == 400
    )


def test_sheets_owned_tabs_literal_values_notes_preserved_and_idempotent(store):
    make_real(store)
    write_secrets(store.root, {"SHEET_ID": "test-sheet-id-12345"})
    tabs, metadata, batches = {}, [], []

    def handler(request):
        nonlocal metadata
        url = str(request.url)
        if request.method == "GET" and "?fields=" in url:
            return httpx.Response(200, json={"sheets": [{"properties": v} for v in tabs.values()]})
        if url.endswith("developerMetadata:search"):
            return httpx.Response(200, json={"matchedDeveloperMetadata": metadata})
        if "/values/" in url:
            return httpx.Response(
                200, json={"values": [["developer", "Наш клиент", "client", "Есть договор"]]}
            )
        data = json.loads(request.content)
        batches.append(data["requests"])
        for item in data["requests"]:
            if "addSheet" in item:
                p = item["addSheet"]["properties"]
                tabs[p["title"]] = p
            if "createDeveloperMetadata" in item:
                metadata.append(
                    {"developerMetadata": item["createDeveloperMetadata"]["developerMetadata"]}
                )
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        for _ in range(2):
            assert (
                sheets.sync(store, client=client, token_provider=lambda root: "dummy")["tabs"] == 8
            )
    assert len(tabs) == 8 and len(batches) == 2
    assert not any("addSheet" in r for r in batches[1])
    manual = {tabs[sheets.PREFIX + name]["sheetId"] for name in ["Заметки", "Стоп-лист"]}
    assert not any(
        r.get("updateCells", {}).get("range", {}).get("sheetId") in manual for r in batches[1]
    )
    assert "formulaValue" not in json.dumps(batches)
    assert stoplist.entries(store)[0]["value"] == "Наш клиент"


def test_sheets_foreign_tab_refused_before_write(store):
    write_secrets(store.root, {"SHEET_ID": "test-sheet-id-12345"})

    def handler(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "sheets": [{"properties": {"title": sheets.PREFIX + "Реестр", "sheetId": 1}}]
                },
            )
        assert str(request.url).endswith("developerMetadata:search")
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="не принадлежит"):
            sheets.sync(store, client=client, token_provider=lambda root: "dummy")


def test_service_account_untrusted_endpoints_rejected():
    value = {
        "type": "service_account",
        "client_email": "x@p.iam.gserviceaccount.com",
        "token_uri": "https://evil.test/token",
        "private_key": "-----BEGIN PRIVATE KEY-----",
    }
    with pytest.raises(ValueError):
        sheets.validate_credentials(json.dumps(value))
    value.update(token_uri="https://oauth2.googleapis.com/token", universe_domain="evil.test")
    with pytest.raises(ValueError):
        sheets.validate_credentials(json.dumps(value))


def test_weekly_steps_failure_isolated_and_completed_steps_not_repeated(store):
    make_real(store)
    registry.bind(store, "demo-1", registry.RegistryBinding(house_id="12345", confirmed=True))
    calls = []

    def bad(*args):
        calls.append("registry")
        raise ValueError("secret must not appear")

    def good(*args):
        calls.append("sheets")

    cfg = {"sync_registry": True, "sync_sheets": True}
    first = complete_steps(store, "week1", cfg, bad, good)
    assert len(first) == 2 and "secret" not in json.dumps(first)
    assert complete_steps(store, "week1", cfg, bad, good) == first
    assert calls == ["registry", "sheets"]
    assert not settings(store)["sync_registry"] and not settings(store)["sync_sheets"]
