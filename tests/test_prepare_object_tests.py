import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/prepare_object_tests.py"
spec = importlib.util.spec_from_file_location("prepare_object_tests", SCRIPT)
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


@pytest.fixture
def workspace(tmp_path):
    folder = tmp_path / "data/local"
    folder.mkdir(parents=True)
    db = sqlite3.connect(folder / "workspace.sqlite3")
    db.executescript("""
        CREATE TABLE records(id TEXT PRIMARY KEY,dataset TEXT,payload TEXT,status TEXT,note TEXT,
          revision INTEGER,updated_at TEXT,stage INTEGER,situation TEXT,topics TEXT);
        CREATE TABLE events(id INTEGER PRIMARY KEY,record_id TEXT,action TEXT,
          detail TEXT,created_at TEXT);
        CREATE TABLE object_questions(id INTEGER PRIMARY KEY,record_id TEXT,question TEXT);
    """)
    for index, ident in enumerate(prepare.TARGET_IDS):
        payload = {
            "id": ident,
            "name": "Исходное имя",
            "claims": {"общий": "первый" if index == 0 else "второй"},
            "urls": ["https://example.org/common", f"https://example.org/{index}"],
            "sources": [{"url": "https://example.org/common"}, {"row": index}],
            "questions": ["Когда ключи?"],
            "cells": ["original", index],
            "file": "source.xlsx",
            "sheet": "Исходный лист",
            "row": index + 5,
            "summary": "Старое положительное утверждение",
            "eisgs_id_hints": ["old-id"],
        }
        db.execute(
            "INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                ident,
                "web" if index == 2 else "imported",
                json.dumps(payload, ensure_ascii=False),
                "inwork",
                "Исходная заметка",
                7,
                "2026-01-01",
                2,
                "сроки",
                "[]",
            ),
        )
        db.execute(
            "INSERT INTO object_questions(record_id,question) VALUES(?,?)",
            (ident, "Исходный вопрос"),
        )
    db.commit()
    db.close()
    return tmp_path


def snapshot(workspace):
    with sqlite3.connect(workspace / "data/local/workspace.sqlite3") as db:
        db.row_factory = sqlite3.Row
        return {
            "records": {row["id"]: dict(row) for row in db.execute("SELECT rowid,* FROM records")},
            "events": [tuple(row) for row in db.execute("SELECT * FROM events")],
            "questions": [tuple(row) for row in db.execute("SELECT * FROM object_questions")],
        }


def test_dry_run_is_read_only_and_apply_is_idempotent_without_extra_events_or_questions(workspace):
    before = snapshot(workspace)
    dry = prepare.prepare_workspace(workspace)
    assert dry["changed"] == 3 and not dry["applied"]
    assert snapshot(workspace) == before
    assert prepare.prepare_workspace(workspace, apply=True)["changed"] == 3
    first = snapshot(workspace)
    assert prepare.prepare_workspace(workspace, apply=True)["changed"] == 0
    assert snapshot(workspace) == first
    assert len(first["events"]) == 3 and first["questions"] == before["questions"]
    for ident in prepare.TARGET_IDS:
        for key in ("rowid", "status", "note", "stage", "situation", "topics"):
            assert first["records"][ident][key] == before["records"][ident][key]


def test_merge_preserves_raw_imports_claims_urls_sources_and_exact_scope(workspace):
    before = snapshot(workspace)
    prepare.prepare_workspace(workspace, apply=True)
    records = snapshot(workspace)["records"]
    canonical = json.loads(records[prepare.KROST_ID]["payload"])
    assert records[prepare.KROST_ID]["dataset"] == "web"
    assert canonical["name"] == "Новая Звезда — II очередь"
    assert canonical["eisgs_id_hints"] == ["66399"]
    assert canonical["report_scope"] == {
        "house_id": "66399",
        "phase": "II очередь",
        "exclude_locations": ["Расплетина 14", "I очередь сдана"],
    }
    assert canonical["claims"]["общий"] == "первый\nвторой"
    assert len(canonical["urls"]) == 3 and len(canonical["sources"]) == 3
    assert canonical["questions"] == ["Когда ключи?"]
    assert len(canonical["source_imports"]) == 2
    for source in canonical["source_imports"]:
        assert source["payload"] == json.loads(before["records"][source["record_id"]]["payload"])
        assert source["status"] == "inwork" and source["note"] == "Исходная заметка"
    duplicate = json.loads(records[prepare.KROST_DUPLICATE_ID]["payload"])
    assert duplicate.pop("merged_into") == prepare.KROST_ID
    assert duplicate == json.loads(before["records"][prepare.KROST_DUPLICATE_ID]["payload"])
    dogma = json.loads(records[prepare.DOGMA_ID]["payload"])
    assert set(prepare.DOGMA_SEEDS) <= set(dogma["urls"])
    assert dogma["source_imports"][0]["payload"]["summary"] == "Старое положительное утверждение"
    assert dogma["claims"] == json.loads(before["records"][prepare.DOGMA_ID]["payload"])["claims"]


def test_missing_target_rolls_back_without_partial_preparation(workspace):
    with sqlite3.connect(workspace / "data/local/workspace.sqlite3") as db:
        db.execute("DELETE FROM records WHERE id=?", (prepare.DOGMA_ID,))
    before = snapshot(workspace)
    with pytest.raises(ValueError, match="Не все три"):
        prepare.prepare_workspace(workspace, apply=True)
    assert snapshot(workspace) == before


def test_failed_event_write_rolls_back_all_previous_record_mutations(workspace):
    with sqlite3.connect(workspace / "data/local/workspace.sqlite3") as db:
        db.execute(
            "CREATE TRIGGER reject_second_event BEFORE INSERT ON events "
            "WHEN NEW.record_id='" + prepare.KROST_DUPLICATE_ID + "' "
            "BEGIN SELECT RAISE(ABORT,'test failure'); END"
        )
    before = snapshot(workspace)
    with pytest.raises(sqlite3.IntegrityError, match="test failure"):
        prepare.prepare_workspace(workspace, apply=True)
    assert snapshot(workspace) == before
