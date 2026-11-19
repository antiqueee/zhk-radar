import json
import shutil

import pytest

from radar.web.coverage import for_record
from radar.web.dossiers import dossier
from radar.web.record_merge import merge_project, revert_project_merge
from radar.web.sources import initialize_sources
from radar.web.store import Store, now


@pytest.fixture
def store(tmp_path):
    shutil.copytree("config", tmp_path / "config")
    result = Store(tmp_path)
    initialize_sources(result)
    return result


def card(store, ident, url, *, building=None):
    payload = {"name": "ЖК Учебный квартал", "urls": [url], "building": building}
    with store.connect() as db:
        db.execute(
            "INSERT INTO records(id,dataset,payload,updated_at) VALUES(?, 'web', ?, ?)",
            (ident, json.dumps(payload), now()),
        )


def signal(store, ident, key, quote, url, *, building=None):
    payload = {
        "quote": quote, "source_url": url, "source_kind": "page", "source_id": url,
        "event_type": "defects", "event_date": None, "published_at": None,
        "building": building, "topics": [],
    }
    with store.connect() as db:
        db.execute("INSERT INTO record_signals VALUES(?,?,?,?,?)", (
            key, ident, url, json.dumps(payload), now(),
        ))
        db.execute(
            "INSERT INTO evidence(record_id,title,url,quote,source_text,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (ident, "Источник", url, quote, quote, now()),
        )


def test_confirmed_merge_pools_materials_and_can_be_undone(store):
    project = "https://project.test/overview"
    article = "https://review.test/article"
    card(store, "project", project)
    card(store, "building-a", article + "?utm_source=a", building="корпус 3.1")
    card(store, "building-copy", article, building="корпус 3.1")
    card(store, "same-name-other-city", "https://fixture.test/other")
    signal(store, "project", "existing", "Описание площадки", project)
    quote = "Есть жалобы на задержку выдачи ключей"
    signal(store, "building-a", "one", quote, article + "?utm_source=a", building="корпус 3.1")
    signal(store, "building-copy", "duplicate", quote, article, building="корпус 3.1")
    signal(store, "building-copy", "unrelated", "Из другого ЖК", article)
    members = ["building-a", "building-copy"]
    options = {"identity": {"region": "Московская область"}, "exclude_signals": ["unrelated"]}
    preview = merge_project(store, "project", members, **options)
    assert preview["members_to_link"] == members and preview["signals_to_copy"] == 1
    assert preview["evidence_to_copy"] == 1 and preview["sources"] == 2
    assert not store.get("building-a").get("merged_into")

    applied = merge_project(store, "project", members, apply=True, **options)
    assert all(store.get(ident)["merged_into"] == "project" for ident in members)
    assert not store.get("same-name-other-city").get("merged_into")
    copied = next(row for row in dossier(store, "project")["signals"] if row["quote"] == quote)
    assert copied["building"] == "корпус 3.1" and copied["origin_record_id"] == "building-a"
    quotes = {row["quote"] for row in store.get("project")["evidence"]}
    assert quotes == {"Описание площадки", quote}
    assert for_record(store, "project")["publications"] == 2
    assert not merge_project(store, "project", members, apply=True, **options)["changed"]

    assert revert_project_merge(store, applied["merge_id"])
    assert not revert_project_merge(store, applied["merge_id"])
    assert not store.get("building-a").get("merged_into")
    assert len(store.get("project")["evidence"]) == 1
    assert len(store.get("building-copy")["evidence"]) == 2


def test_merge_keeps_a_member_with_existing_work_and_preserves_parent_decision(store):
    card(store, "project", "https://project.test/")
    card(store, "separate", "https://project.test/building-2", building="корпус 2")
    with store.connect() as db:
        db.execute("UPDATE records SET status='review',note='Решение оператора' WHERE id='project'")
    brief = store.save_brief("separate", {"report": "Сохранённый отчёт"})

    result = merge_project(store, "project", ["separate"], apply=True)
    assert result["kept_separate"] == {"separate": ["briefs"]} and not result["changed"]
    assert store.get("project")["note"] == "Решение оператора"
    assert store.get("project")["status"] == "review"
    assert not store.get("separate").get("merged_into")
    with store.connect() as db:
        row = db.execute("SELECT record_id FROM briefs WHERE id=?", (brief["id"],)).fetchone()
        assert row[0] == "separate"
