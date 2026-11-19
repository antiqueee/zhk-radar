import json
import sqlite3
from contextlib import contextmanager

from radar.web import coverage


class Store:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def connect(self):
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            yield db


def test_missing_tables_return_zero_without_creating_any(tmp_path):
    store = Store(tmp_path / "empty.sqlite")
    assert coverage.for_record(store, "one")["publications"] == 0
    assert coverage.all_records(store) == {}
    with store.connect() as db:
        assert list(db.execute("SELECT name FROM sqlite_master WHERE type='table'")) == []


def test_loaded_report_documents_count_unique_urls_without_becoming_proof(tmp_path):
    store = Store(tmp_path / "coverage.sqlite")
    documents = [
        {
            "url": f"https://msk1.ru/article/?utm_source={i}#part",
            "read_status": "loaded",
            "source_kind": "page",
            "text": "Материал",
        }
        for i in range(25)
    ] + [
        {
            "url": "https://ria.ru/second",
            "read_status": "loaded",
            "source_kind": "page",
            "text": "Другой материал",
        },
        {"url": "https://blocked.example/", "read_status": "protected", "text": "Сниппет"},
        {
            "url": "https://snippet.example/",
            "read_status": "loaded",
            "source_kind": "snippet",
            "text": "Сниппет",
        },
        {
            "url": "https://protected.example/",
            "read_status": "loaded",
            "protected": True,
            "text": "Вход",
        },
        {"url": "https://empty.example/", "read_status": "loaded", "text": ""},
    ]
    with store.connect() as db:
        db.execute("CREATE TABLE report_research(record_id TEXT PRIMARY KEY,payload TEXT)")
        db.execute(
            "INSERT INTO report_research VALUES(?,?)", ("one", json.dumps({"documents": documents}))
        )
        db.execute(
            "INSERT INTO report_research VALUES(?,?)",
            ("other", json.dumps({"documents": documents[:1]})),
        )
    result = coverage.for_record(store, "one")
    assert result["publications"] == 2
    assert result["sites"] == 2
    assert result["quote_groups"] == 0
    assert result["multiple"] is False
    assert "не подтверждены автоматически" in result["note"]
    assert coverage.all_records(store)["one"] == result
    assert coverage.for_record(store, "unknown")["publications"] == 0


def test_existing_quotes_keep_their_grouping_and_deduplicate_loaded_page(tmp_path):
    store = Store(tmp_path / "mixed.sqlite")
    with store.connect() as db:
        db.execute("CREATE TABLE evidence(record_id TEXT,url TEXT,quote TEXT)")
        db.execute("CREATE TABLE report_research(record_id TEXT,payload TEXT)")
        db.executemany(
            "INSERT INTO evidence VALUES(?,?,?)",
            [
                (
                    "one",
                    "https://msk1.ru/article/",
                    "Срок передачи корпуса перенесли на март следующего года",
                ),
                (
                    "one",
                    "https://ria.ru/second",
                    "Дольщики подписали коллективное обращение в надзорные органы",
                ),
            ],
        )
        db.execute(
            "INSERT INTO report_research VALUES(?,?)",
            (
                "one",
                json.dumps(
                    {
                        "documents": [
                            {
                                "url": "https://msk1.ru/article/?utm_source=search",
                                "read_status": "loaded",
                                "text": "Полная страница",
                            }
                        ]
                    }
                ),
            ),
        )
    result = coverage.for_record(store, "one")
    assert result["publications"] == 2
    assert result["quote_groups"] == 2
    assert result["multiple"] is True
    assert result == coverage.all_records(store)["one"]
