import io
import shutil
import time
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from radar.web.app import create_app
from radar.web.demo import run_demo
from radar.web.discovery import Finding, ScanCancelled, SearchRequest, discover
from radar.web.dossiers import dossier, object_key, supported_date
from radar.web.jobs import JobRunner
from radar.web.store import Store


@pytest.fixture
def store(tmp_path):
    shutil.copytree(Path("config"), tmp_path / "config")
    return Store(tmp_path)


def finish(client, ident):
    for _ in range(300):
        job = client.get("/api/jobs/" + ident).json()
        if job["status"] not in {"queued", "running"}:
            return job
        time.sleep(0.01)
    pytest.fail("Worker did not complete")


def test_offline_end_to_end_job_and_pdf(store):
    with TestClient(create_app(store.root), headers={"X-Radar-Local": "1"}) as client:
        response = client.post("/api/search", json={"region": "capitals", "mode": "demo"})
        assert response.status_code == 202
        job = finish(client, response.json()["id"])
        assert job["status"] == "completed", job
        assert job["result"]["found"] == 2
        assert job["result"]["signals_added"] == 3
        assert job["result"]["external_requests"] == 0
        assert [e["percent"] for e in job["events"]] == sorted(e["percent"] for e in job["events"])
        candidates = [
            r
            for r in client.get("/api/records?dataset=demo").json()
            if r["id"].startswith("demo-web-")
        ]
        assert len(candidates) == 2
        record = next(r for r in candidates if r["building"] == "корпус 3")
        detail = client.get("/api/records/" + record["id"]).json()
        assert len(detail["dossier"]["signals"]) == 2
        assert detail["dossier"]["full_pages"] == 2
        source = client.get("/api/sources/" + detail["dossier"]["sources"][0]["id"])
        assert "Учебный берег" in source.json()["text"]
        brief = client.post("/api/records/" + record["id"] + "/brief")
        assert brief.status_code == 200, brief.text
        pdf = client.get(f"/api/briefs/{brief.json()['id']}/pdf")
        assert len(PdfReader(io.BytesIO(pdf.content)).pages) >= 6
        repeat = client.post("/api/search", json={"region": "capitals", "mode": "demo"}).json()
        repeated = finish(client, repeat["id"])
        assert repeated["result"]["added"] == repeated["result"]["signals_added"] == 0
    with TestClient(create_app(store.root)) as restored:
        assert restored.get("/api/jobs/latest").json()["id"] == repeat["id"]
        assert len(restored.get("/api/records/" + record["id"]).json()["dossier"]["signals"]) == 2


def test_queued_duplicate_cancel_and_restart_recovery(store):
    runner = JobRunner(store)
    req = SearchRequest(region="moscow")
    first = runner.submit(req, "demo")
    assert runner.submit(req, "demo")["id"] == first["id"]
    with pytest.raises(ValueError, match="Другой поиск"):
        runner.submit(SearchRequest(region="russia"), "demo")
    assert runner.cancel(first["id"])["status"] == "cancelled"
    second = runner.submit(req, "demo")
    with store.connect() as db:
        db.execute("UPDATE scan_jobs SET status='running' WHERE id=?", (second["id"],))
    runner.start()
    try:
        assert runner.get(second["id"])["status"] == "interrupted"
        other = JobRunner(store)
        with pytest.raises(RuntimeError, match="уже запущен"):
            other.start()
    finally:
        runner.close()


def test_cancel_boundary_preserves_saved_work(store):
    def cancelled():
        return True

    with pytest.raises(ScanCancelled):
        run_demo(store, SearchRequest(region="moscow"), lambda *args: None, cancelled)
    assert not [r for r in store.records("demo") if r["id"].startswith("demo-web-")]


def finding(**changes):
    return Finding(
        name="ЖК «Северный»",
        region="Москва",
        summary="В публикации есть жалобы жителей.",
        source_index=0,
        quote="Жители сообщают о переносе передачи квартир.",
        **changes,
    )


def test_conservative_identity_and_dates():
    a = finding(building="корпус 3")
    assert object_key(a, "a") == object_key(a.model_copy(update={"name": "Северный"}), "b")
    assert object_key(a, "a") != object_key(a.model_copy(update={"building": "корпус 4"}), "a")
    assert object_key(a, "a") != object_key(a.model_copy(update={"region": "Казань"}), "a")
    assert object_key(a.model_copy(update={"region": ""}), "a") != object_key(
        a.model_copy(update={"region": ""}), "b"
    )
    assert supported_date(date(2026, 1, 10), "10 января 2026 года") == "2026-01-10"
    assert supported_date(date(2026, 1, 10), "10.01.2026") == "2026-01-10"
    assert supported_date(date(2026, 1, 10), "В январе") is None
    assert supported_date(date(2099, 1, 10), "10.01.2099") is None


def test_unavailable_page_is_labelled_and_does_not_stop_other_pages(store):
    from radar.web.demo import EXAMPLES
    from radar.web.discovery import Findings
    from radar.web.sources import Page, SourceError
    from radar.web.store import now

    examples = EXAMPLES[:2]
    documents = [
        dict(url=x["url"], title=x["title"], text=x["quote"], query="test") for x in examples
    ]

    def reader(url):
        if url == examples[1]["url"]:
            raise SourceError("blocked")
        return Page(
            url, url, "loaded", text=examples[0]["quote"], fetched_at=now(), content_hash="full"
        )

    def extract(batch):
        return Findings.model_validate(
            {
                "objects": [
                    dict(
                        name=example["name"],
                        region="Москва",
                        building="корпус 3",
                        summary="По публикации жители сообщают о проблеме.",
                        quote=example["quote"],
                        source_index=i,
                    )
                    for i, example in enumerate(examples)
                ]
            }
        )

    result = discover(
        store,
        SearchRequest(region="moscow"),
        reader=reader,
        dataset="demo",
        prepared={"documents": documents, "extract": extract},
    )
    assert result["status"] == "partial"
    assert result["full_pages"] == result["snippets"] == 1
    assert result["found"] == 1 and result["signals_added"] == 2
    record = next(r for r in store.records("demo") if r["id"].startswith("demo-web-"))
    assert dossier(store, record["id"])["snippets"] == 1


def test_running_cancel_is_cooperative_and_durable(store, monkeypatch):
    from radar.web import jobs
    from radar.web.configuration import SettingsUpdate, save_settings

    save_settings(
        store.root,
        SettingsUpdate(
            values={
                "YANDEX_SEARCH_API_KEY": "x",
                "YANDEX_FOLDER_ID": "x",
                "OPENROUTER_API_KEY": "x",
            },
            model="test/extract",
        ),
    )

    def work(store, request, progress, cancelled, run_id=None):
        progress("pages", 25, "Читаем источник")
        for _ in range(300):
            if cancelled():
                raise ScanCancelled()
            time.sleep(0.01)
        pytest.fail("Worker did not observe cancellation")

    monkeypatch.setattr(jobs, "discover", work)
    runner = JobRunner(store)
    runner.start()
    try:
        job = runner.submit(SearchRequest(region="moscow"))
        for _ in range(300):
            if runner.get(job["id"])["stage"] == "pages":
                break
            time.sleep(0.01)
        runner.cancel(job["id"])
        for _ in range(300):
            if runner.get(job["id"])["status"] == "cancelled":
                break
            time.sleep(0.01)
        assert runner.get(job["id"])["status"] == "cancelled"
        assert runner.get(job["id"])["result"] is None
    finally:
        runner.close()


def test_region_inflections_are_normalized_only_with_text_support():
    from radar.web.dossiers import source_region

    assert source_region("Москва", "Жители в Москве") == "Москва"
    assert source_region("Московская область", "В Московской области") == "Московская область"
    assert source_region("Москва", "Новостройки в Казани") == "Не установлен"
    assert source_region("Московская область", "Новостройки Москвы") == "Не установлен"
