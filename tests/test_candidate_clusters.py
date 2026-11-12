import json
import shutil

import pytest
from fastapi.testclient import TestClient

from radar.web import coverage, stoplist, workflow
from radar.web.app import create_app
from radar.web.configuration import write_secrets
from radar.web.discovery import Finding, Findings
from radar.web.dossiers import persist_findings
from radar.web.operations import OperationsSettings, save, usage
from radar.web.sources import Page
from radar.web.store import Store, now


@pytest.fixture
def store(tmp_path):
    shutil.copytree("config", tmp_path / "config")
    value = Store(tmp_path)
    write_secrets(
        value.root,
        {
            "YANDEX_SEARCH_API_KEY": "fixture",
            "YANDEX_FOLDER_ID": "fixture",
            "OPENROUTER_API_KEY": "fixture",
        },
    )
    return value


def candidate(store, number=1):
    name = f"Учебный квартал {number}"
    text = f"Москва. ЖК {name}: жители сообщают о переносе выдачи ключей."
    finding = Finding(
        name=name,
        region="Москва",
        summary=text,
        quote=text,
        source_index=0,
        event_type="handover_delay",
    )
    doc = {
        "url": f"https://fixture.test/{number}",
        "text": text,
        "title": name,
        "source_kind": "snippet",
        "source_id": f"fixture-{number}",
    }
    return persist_findings(store, [finding], [doc])["record_ids"][0]


def test_hide_survives_rediscovery_and_restore_is_reversible(store):
    ident = candidate(store)
    client = TestClient(create_app(store.root), headers={"X-Radar-Local": "1"})
    response = client.post(
        "/api/stoplist",
        json={
            "kind": "record",
            "value": ident,
            "reason": "refused",
            "note": "Не наш профиль",
        },
    )
    assert response.status_code == 200
    candidate(store)
    reopened = Store(store.root)
    assert stoplist.blocked(reopened, reopened.get(ident))
    assert workflow.score(reopened, ident)["excluded"]
    row = next(r for r in client.get("/api/records?dataset=web").json() if r["id"] == ident)
    assert row["blocked"]["kind"] == "record"
    assert row["source_coverage"]["publications"] == 1
    client.delete("/api/stoplist/" + row["blocked"]["id"])
    assert not stoplist.blocked(reopened, reopened.get(ident))
    assert len(reopened.get(ident)["evidence"]) == 1
    assert not workflow.score(reopened, ident)["excluded"]


def test_reprints_are_not_a_multi_source_cluster():
    quote = "Жители учебного квартала сообщают о повторном переносе выдачи ключей."
    items = [{"url": f"https://{host}.test/a", "quote": quote} for host in ["one", "two"]]
    assert coverage.summarize(items)["publications"] == 2
    assert not coverage.summarize(items)["multiple"]
    items.append(
        {
            "url": "https://official.test/a",
            "quote": "Компания опубликовала календарь поэтапной приёмки помещений в декабре.",
        }
    )
    assert coverage.summarize(items)["multiple"]


def test_cluster_then_deep_reuses_search_and_caps_input(store, monkeypatch):
    ident = candidate(store)
    searches, inputs = [], []
    text = store.get(ident)["summary"]

    def search(client, cfg, query, period):
        searches.append(query)
        return [
            {
                "url": f"https://site{n}.test/{len(searches)}",
                "title": "Учебный квартал 1",
                "text": text,
                "query": query,
            }
            for n in range(4)
        ]

    def reader(url):
        return Page(
            url,
            url,
            "loaded",
            text=text + " " + "Контекст. " * 3000,
            title="Учебный квартал 1",
            fetched_at=now(),
        )

    def extractor(client, cfg, model, docs):
        inputs.append(sum(len(d["text"]) for d in docs))
        return Findings(
            objects=[
                Finding(
                    name="Учебный квартал 1",
                    region="Москва",
                    summary=text,
                    quote=text,
                    source_index=i,
                    event_type="handover_delay",
                )
                for i in range(len(docs))
            ]
        )

    monkeypatch.setattr(workflow, "yandex", search)
    kwargs = dict(client=object(), reader=reader, extractor=extractor)
    first = workflow.research(store, ident, "one", depth="cluster", **kwargs)
    second = workflow.research(store, ident, "one", depth="deep", **kwargs)
    before = usage(store)["calls"]
    repeated = workflow.research(store, ident, "two", depth="deep", **kwargs)
    assert first["questions_completed"] == 2
    assert second["questions_completed"] == 6 and second["search_cache_hits"] == 2
    assert repeated["search_cache_hits"] == 6
    assert len(searches) == 6 and usage(store)["calls"] == before
    assert len(inputs) == 2 and inputs[0] <= 32000 and inputs[1] <= 64000
    assert coverage.all_records(store)[ident]["publications"] > 1


def test_query_documents_prefers_different_sites(store, monkeypatch):
    monkeypatch.setattr(
        workflow,
        "yandex",
        lambda *a: [
            {"url": url, "title": "fixture", "text": "fixture"}
            for url in ["https://one.test/a", "https://one.test/b", "https://two.test/a"]
        ],
    )
    docs, _ = workflow.query_documents(
        store,
        "fixture",
        "run",
        object(),
        reader=lambda url: Page(url, url, "loaded", text="fixture", fetched_at=now()),
        max_pages=2,
    )
    assert [d["url"] for d in docs] == ["https://one.test/a", "https://two.test/a"]


def test_partial_research_preserves_pages_when_budget_exhausted(store, monkeypatch):
    ident = candidate(store)
    save(store, OperationsSettings(per_run=1))
    monkeypatch.setattr(
        workflow,
        "yandex",
        lambda *a: [{"url": "https://one.test/a", "title": "fixture", "text": "fixture"}],
    )
    result = workflow.research(
        store,
        ident,
        "limited",
        client=object(),
        depth="cluster",
        reader=lambda url: Page(url, url, "loaded", text="fixture", fetched_at=now()),
        extractor=lambda *a: pytest.fail("Budget forbids the model call"),
    )
    assert result["status"] == "partial"
    assert len(result["documents"]) == 1 and result["questions_completed"] == 1
    assert usage(store)["calls"] == 1


def test_two_stage_queue_rotates_and_excludes_hidden(store, monkeypatch):
    ids = [candidate(store, n) for n in range(12)]
    stoplist.add(store, stoplist.StopEntry(kind="record", value=ids[0], reason="refused"))
    visited = []

    def researcher(store, ident, run_id, **kwargs):
        depth = kwargs["depth"]
        visited.append((ident, depth))
        payload = {"depth": depth, "related_ids": [], "signals_added": 0}
        with store.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO research_runs VALUES(?,?,?,?)",
                (ident, run_id, json.dumps(payload), now()),
            )
        return payload

    monkeypatch.setattr(
        "radar.web.screening.screen",
        lambda *a, **kw: {
            "records": [],
            "status": "completed",
        },
    )
    result = workflow.complete_scan(
        store, {"record_ids": ids, "status": "completed"}, "one", researcher=researcher
    )
    assert result["object_research"]["checked"] == 10
    assert result["object_research"]["deepened"] == 3
    assert result["object_research"]["deferred"] == 1
    assert all(ident != ids[0] for ident, _ in visited)
    unseen = next(ident for ident in ids[1:] if (ident, "cluster") not in visited)
    visited.clear()
    workflow.complete_scan(
        store, {"record_ids": ids, "status": "completed"}, "two", researcher=researcher
    )
    assert visited[0] == (unseen, "cluster")


def test_developer_exclusion_cannot_be_lifted_by_restoring_one_object(store):
    ident = candidate(store)
    record = store.get(ident)
    stoplist.add(store, stoplist.StopEntry(kind="developer", value=record["developer"]))
    stoplist.add(store, stoplist.StopEntry(kind="record", value=ident))
    assert stoplist.blocked(store, record)["kind"] == "developer"


def test_cluster_pass_does_not_replace_detailed_answers(store):
    ident = candidate(store)
    workflow.initialize(store)
    deep = {"depth": "deep", "answers": [{"question": "causes", "quote": "earlier source"}]}
    with store.connect() as db:
        db.execute(
            "INSERT INTO research_runs VALUES(?,?,?,?)",
            (ident, "deep", json.dumps(deep), "2026-11-01"),
        )
        db.execute(
            "INSERT INTO research_runs VALUES(?,?,?,?)",
            (ident, "cluster", json.dumps({"depth": "cluster", "answers": []}), now()),
        )
    assert workflow.latest_research(store, ident) == deep
