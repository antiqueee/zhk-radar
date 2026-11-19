import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from shutil import copytree

import pytest
from fastapi.testclient import TestClient

from radar.reports.pipeline_v8 import registry_sources
from radar.web.app import create_app
from radar.web.brand_analytics_sync import BrandAnalyticsSync, Connection
from radar.web.brand_analytics_transport import MessagePage, TransportError
from radar.web.brief_service import payload_for
from radar.web.store import Store, now

_URL = "https://api.brandanalytics.ru/documented/feed"
_CLOCK = datetime(2026, 11, 18, 8, tzinfo=UTC)
_API = "/api/settings/brand-analytics"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    for key in ("ENDPOINT", "TOKEN", "TOPIC_IDS", "STATIC_PARAMS"):
        monkeypatch.delenv("BRAND_ANALYTICS_" + key, raising=False)
    copytree(Path(__file__).resolve().parents[1] / "config", tmp_path / "config")
    return tmp_path


def test_topic_budgets_and_cursor_resume_across_days(workspace):
    calls = []

    def fetch(feed, *, topic_id, since, until, cursor):
        calls.append((topic_id, since, until, cursor))
        if topic_id == "secondary":
            return MessagePage([], None, 0)
        number = int(cursor or "0") + 1
        return MessagePage([], str(number) if number < 6 else None, 0)

    store = Store(workspace)
    sync = BrandAnalyticsSync(store, fetch=fetch)
    saved = sync.save(Connection(endpoint=_URL, topic_ids="main,secondary", token="test-token"))
    assert saved["configured"] and saved["last_run"] is None and not calls
    assert "test-token" not in json.dumps(saved)
    assert sync.submit(_CLOCK)["last_run"]["state"] == "queued"
    sync.tick(_CLOCK)
    result = sync.status()["last_run"]
    assert result["state"] == "partial" and result["result"]["attempts"] == 5
    assert [call[3] for call in calls] == [None, "1", "2", "3", None]
    sync.submit(_CLOCK + timedelta(hours=1))
    sync.tick(_CLOCK + timedelta(hours=1))
    assert len(calls) == 5
    next_day = _CLOCK + timedelta(days=1)
    sync.submit(next_day)
    sync.tick(next_day)
    assert calls[5] == ("main", _CLOCK - timedelta(days=2), _CLOCK, "4")
    assert sync.status()["last_run"]["state"] == "completed"


def test_daily_opt_in_retains_checkpoint_after_provider_error(workspace):
    calls = []

    def fetch(feed, *, topic_id, since, until, cursor):
        calls.append((until, cursor))
        if len(calls) == 1:
            return MessagePage([], "page-2", 0)
        if len(calls) == 2:
            raise TransportError("Лимит запросов Brand Analytics", status_code=429, retryable=True)
        return MessagePage([], None, 0)

    sync = BrandAnalyticsSync(Store(workspace), fetch=fetch)
    sync.save(Connection(endpoint=_URL, topic_ids="main", token="test-token", scheduled=True))
    sync.tick(_CLOCK)
    assert sync.status()["last_run"]["result"]["error"] == "Лимит запросов Brand Analytics"
    sync.tick(_CLOCK + timedelta(minutes=1))
    assert len(calls) == 2
    sync.tick(_CLOCK + timedelta(days=1))
    assert calls[-1] == (_CLOCK, "page-2")
    assert sync.status()["last_run"]["state"] == "completed"


def test_key_correction_can_retry_on_same_day_without_resetting_budget(workspace):
    attempts = []

    def fetch(feed, **request):
        attempts.append(request["since"])
        if len(attempts) == 1:
            raise TransportError("Недействительный ключ", status_code=401)
        return MessagePage([], None, 0)

    sync = BrandAnalyticsSync(Store(workspace), fetch=fetch)
    connection = Connection(endpoint=_URL, topic_ids="primary", token="expired")
    sync.save(connection)
    sync.submit(_CLOCK)
    sync.tick(_CLOCK)
    assert sync.status()["last_run"]["state"] == "failed"
    sync.save(connection.model_copy(update={"token": "valid"}))
    sync.submit(_CLOCK + timedelta(minutes=1))
    sync.tick(_CLOCK + timedelta(minutes=1))
    assert attempts[0] == attempts[1]
    result = sync.status()["last_run"]
    assert result["state"] == "completed" and result["result"]["attempts"] == 2


def test_connection_and_export_reach_cards_and_v8_sources(workspace):
    app = create_app(workspace)
    store = app.state.store
    data = {
        "name": "Nagatino I-Land", "developer": "Эталон", "region": "Москва",
        "aliases": ["Нагатино Ай-Лэнд"], "file": "", "sheet": "", "row": 1,
        "summary": "", "check": "", "assessment": {}, "urls": [], "claims": {},
        "granularity": "project",
    }
    with store.connect() as db:
        db.execute(
            "INSERT INTO records(id,dataset,payload,updated_at) VALUES(?,?,?,?)",
            ("test-nagatino", "web", json.dumps(data), now()),
        )
    publication = {
        "id": "external-1", "url": "https://example.org/article", "title": "Публикация",
        "text": "Дольщики ЖК «Нагатино Ай-Лэнд» в Москве обсуждают сроки выдачи ключей.",
        "published_at": "2026-01-03T14:30:00+00:00",
    }
    with TestClient(app, headers={"X-Radar-Local": "1"}) as client:
        assert client.get(_API).json()["configured"] is False
        assert client.post(_API + "/sync").status_code == 409
        bad = client.put(_API, json={
            "endpoint": "https://wrong.example/feed", "topic_ids": "one", "token": "private-test",
        })
        assert bad.status_code == 422 and "private-test" not in bad.text
        response = client.put(_API, json={
            "endpoint": _URL, "topic_ids": "one", "token": "private-test",
        })
        assert response.status_code == 200 and "private-test" not in response.text
        assert (workspace / ".env").stat().st_mode & 0o777 == 0o600
        assert client.get(_API + "/queries").json()["covered_cards"] == 1
        for field in ("inserted", "unchanged"):
            body = json.dumps([publication]).encode()
            result = client.post(_API + "/import", files={"file": ("export.json", body)})
            assert result.status_code == 200, result.text
            assert result.json()[field] == 1
        card = client.get("/api/records/test-nagatino").json()
        [source] = card["brand_analytics"]
        assert card["source_coverage"]["publications"] == 1
        assert source["url"] == publication["url"] and "text" not in source
        payload = payload_for(store, "test-nagatino")
        [source] = registry_sources(payload)
        assert source["url"] == publication["url"]
        assert source["published_at"] == "2026-01-03T14:30:00+00:00"
        assert "сроки выдачи ключей" in source["text"]


def test_brand_analytics_uses_remaining_v8_source_slots():
    sources = [
        {"url": f"https://news.example/story/{i}", "source_id": str(i),
         "text": "Оригинальная публикация о ЖК", "contour": "media"}
        for i in range(39)
    ]
    imports = [
        {"url": f"https://posts.example/item/{i}", "source_id": f"imported-{i}",
         "text": "Сообщение дольщика в открытом канале", "contour": "communities",
         "origin": "brand_analytics"}
        for i in range(3)
    ]
    registry = registry_sources({
        "report_research": {"documents": sources}, "brand_analytics_documents": imports,
    })
    assert len(registry) == 40
    assert {row["url"] for row in registry} == {row["url"] for row in [*sources, imports[0]]}
