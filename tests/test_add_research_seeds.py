import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/add_research_seeds.py"
spec = importlib.util.spec_from_file_location("add_research_seeds", SCRIPT)
seeds = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seeds)


@pytest.fixture
def workspace(tmp_path):
    folder = tmp_path / "data/local"
    folder.mkdir(parents=True)
    payload = {
        "name": "Новая Звезда", "urls": ["https://old.test/a", "https://old.test/b?utm_x=1"],
        "passport": {"unchanged": True}, "report_qualification": {"verdict": "watch"},
        "report_pipeline": {"passed": False}, "claims": {"Исходная строка": "Не менять"},
    }
    with sqlite3.connect(folder / "workspace.sqlite3") as db:
        db.executescript("""
            CREATE TABLE records(id TEXT PRIMARY KEY,dataset TEXT,payload TEXT,status TEXT,
              note TEXT,revision INTEGER,updated_at TEXT,stage INTEGER,topics TEXT);
            CREATE TABLE events(id INTEGER PRIMARY KEY,record_id TEXT,action TEXT,
              detail TEXT,created_at TEXT);
        """)
        db.execute("INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?)", (
            "canonical", "imported", json.dumps(payload), "inwork", "Заметка аналитика",
            7, "2026-11-01T10:00:00+00:00", 2, '["выдача_ключей"]',
        ))
    return tmp_path


def snapshot(workspace, path=None):
    with sqlite3.connect(path or workspace / "data/local/workspace.sqlite3") as db:
        db.row_factory = sqlite3.Row
        return {
            "records": [dict(row) for row in db.execute("SELECT * FROM records")],
            "events": [dict(row) for row in db.execute("SELECT * FROM events")],
        }


def test_dry_run_and_apply_preserve_all_fields_and_replay_has_no_effect(workspace):
    urls = ["https://old.test/b", "https://new.test/article?utm_source=test#part"]
    before = snapshot(workspace)
    dry = seeds.add_research_seeds(workspace, "canonical", urls)
    assert dry["changed"] and not dry["applied"] and dry["backup"] is None
    assert snapshot(workspace) == before
    assert not (workspace / "data/local/backups").exists()
    applied = seeds.add_research_seeds(workspace, "canonical", urls, apply=True)
    assert applied["applied"] and Path(applied["backup"]).is_file()
    assert snapshot(workspace, Path(applied["backup"])) == before
    after = snapshot(workspace)
    first, second = before["records"][0], after["records"][0]
    old_payload, payload = json.loads(first["payload"]), json.loads(second["payload"])
    assert payload.pop("urls") == [
        "https://old.test/b?utm_x=1", "https://new.test/article", "https://old.test/a"
    ]
    old_payload.pop("urls")
    assert payload == old_payload
    for field in ("id", "dataset", "status", "note", "stage", "topics"):
        assert first[field] == second[field]
    assert second["revision"] == 8 and len(after["events"]) == 1
    repeated = seeds.add_research_seeds(workspace, "canonical", urls, apply=True)
    assert not repeated["changed"] and not repeated["applied"]
    assert snapshot(workspace) == after
    assert len(list((workspace / "data/local/backups").glob("*.sqlite3"))) == 1


@pytest.mark.parametrize("urls", [[], ["https://example.test"] * 7, ["file:///tmp/private"],
                                 ["https://user:password@example.test/"],
                                 ["http://example.test:8080/"]])
def test_invalid_seeds_do_not_write_or_backup(workspace, urls):
    before = snapshot(workspace)
    with pytest.raises(ValueError):
        seeds.add_research_seeds(workspace, "canonical", urls, apply=True)
    assert snapshot(workspace) == before
    assert not (workspace / "data/local/backups").exists()


def test_missing_record_does_not_create_backup_or_change_workspace(workspace):
    before = snapshot(workspace)
    with pytest.raises(ValueError, match="не найдена"):
        seeds.add_research_seeds(workspace, "missing", ["https://new.test/"], apply=True)
    assert snapshot(workspace) == before
    assert not (workspace / "data/local/backups").exists()


def test_failed_event_write_rolls_back_url_and_revision_and_keeps_verified_backup(workspace):
    before = snapshot(workspace)
    with sqlite3.connect(workspace / "data/local/workspace.sqlite3") as db:
        db.execute("CREATE TRIGGER fail_event BEFORE INSERT ON events "
                   "BEGIN SELECT RAISE(ABORT,'forced offline failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="forced offline failure"):
        seeds.add_research_seeds(workspace, "canonical", ["https://new.test/"], apply=True)
    assert snapshot(workspace) == before
    backup = next((workspace / "data/local/backups").glob("*.sqlite3"))
    assert snapshot(workspace, backup) == before
