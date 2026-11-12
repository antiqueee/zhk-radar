import json
import shutil
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_operations_and_telegram import FakeTelegram

from radar.web import chat_analysis, chat_workflow, screening, workflow
from radar.web.chats import ChatImport, ingest_chat
from radar.web.configuration import SettingsUpdate, save_settings, write_secrets
from radar.web.discovery import ScanCancelled
from radar.web.operations import OperationsSettings, reserve_call, save
from radar.web.store import Store
from radar.web.telegram import TelegramLink, TelegramReader


@pytest.fixture
def store(tmp_path):
    shutil.copytree("config", tmp_path / "config")
    value = Store(tmp_path)
    with value.connect() as db:
        db.execute("UPDATE records SET dataset='web' WHERE id='demo-1'")
    return value


class Reader(TelegramReader):
    def __init__(self, store):
        super().__init__(store, lambda: "salt")
        self.reads, self.inspections = [], []

    def status(self):
        return {"session_saved": True}

    def inspect(self, url):
        self.inspections.append(url)
        record = self.store.get("demo-1")
        return {
            "url": url,
            "title": record["name"],
            "description": record["region"],
            "peer_id": "99",
            "public": True,
            "group": True,
        }

    def sync(self, ident, url, limit=1000, **kwargs):
        assert limit == 1500 and kwargs["recent"]
        self.reads.append((ident, url))
        return {"read": 1400, "ingest": {"added": 1400}, "partial": False}


def candidate(store, url="https://t.me/valid_chat", state="suggested"):
    workflow.initialize(store)
    value = {"title": "Учебный чат", "url": url}
    with store.connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO chat_candidates VALUES(?,?,?,?)",
            ("demo-1", url, json.dumps(value), state),
        )
    return chat_workflow.candidates(store, "demo-1")


def test_auto_discovery_reads_via_api_and_labels_once(store):
    reader = Reader(store)
    calls = []

    def finder(*args):
        calls.append("find")
        return {"candidates": candidate(store)}

    def analyze(store, ident, **kwargs):
        calls.append("model")
        assert kwargs["run_id"] == "run" and kwargs["max_messages"] == 60
        assert kwargs["text_budget"] == 12000 and kwargs["newest_first"]
        reserve_call(store, kwargs["run_id"], "chat-model")
        return {"labelled": 60, "more": True}

    first = screening.screen(store, ["demo-1"], "run", reader, finder=finder, analyzer=analyze)
    second = screening.screen(store, ["demo-1"], "run", reader, finder=finder, analyzer=analyze)
    assert calls == ["find", "model"] and len(reader.reads) == 1
    assert first == second and first["status"] == "partial"
    assert first["records"][0]["period_days"] == 30
    assert reader.links("demo-1")[0]["peer_id"] == "99"
    assert candidate(store)[0]["payload"]["auto_matched"]


def test_read_only_mode_never_calls_model_and_rejected_chat_stays_rejected(store):
    save(store, OperationsSettings(screen_model_calls=0))
    reader = Reader(store)
    candidate(store)

    def forbidden(*a, **kw):
        raise AssertionError("No model or search call expected")

    result = screening.screen(store, ["demo-1"], "read-only", reader, analyzer=forbidden)
    assert len(reader.reads) == 1 and result["records"][0]["analysis_disabled"]
    reader.remove("demo-1", TelegramLink(url="https://t.me/valid_chat"))
    screening.screen(store, ["demo-1"], "removed", reader, finder=forbidden, analyzer=forbidden)
    assert len(reader.reads) == 1 and not reader.links("demo-1")


@pytest.mark.parametrize(
    "change",
    [
        {"public": False},
        {"group": False},
        {"title": "Другой ЖК"},
        {"description": "Санкт-Петербург корпус 3"},
        {"description": "Москва корпус 30"},
        {"description": "Москва"},
    ],
)
def test_ambiguous_or_wrong_chat_never_auto_matches(change):
    record = {"name": "ЖК Учебный берег", "region": "Москва", "building": "корпус 3"}
    meta = {
        "title": "ЖК Учебный берег",
        "description": "Москва корпус 3",
        "public": True,
        "group": True,
    }
    assert screening.matches(record, meta)[0]
    assert not screening.matches(record, {**meta, **change})[0]


def test_budget_limit_does_not_prevent_free_telegram_read(store):
    save(store, OperationsSettings(per_run=1, screen_model_calls=1))
    reserve_call(store, "run", "chat-model")
    reader = Reader(store)
    reader.add("demo-1", TelegramLink(url="https://t.me/valid_chat"))

    def forbidden(*a, **kw):
        raise AssertionError("Model quota exhausted")

    result = screening.screen(store, ["demo-1"], "run", reader, analyzer=forbidden)
    assert reader.reads and result["records"][0]["more_unlabelled"]


def test_disconnected_disabled_and_cancelled_have_no_side_effects(store):
    def forbidden(*a, **kw):
        raise AssertionError("External calls forbidden")

    assert (
        screening.screen(store, ["demo-1"], "run", None, finder=forbidden)["status"]
        == "not_connected"
    )
    save(store, OperationsSettings(screen_chats=False))
    assert (
        screening.screen(store, ["demo-1"], "run", Reader(store), finder=forbidden)["status"]
        == "disabled"
    )
    save(store, OperationsSettings())
    with pytest.raises(ScanCancelled):
        screening.screen(
            store, ["demo-1"], "run", Reader(store), finder=forbidden, cancelled=lambda: True
        )


class OrderedTelegram(FakeTelegram):
    def __init__(self):
        super().__init__()
        self.options = []

    async def iter_messages(self, entity, **kwargs):
        self.options.append(kwargs)
        rows = [m for m in self.messages if m.id > kwargs["min_id"]]
        if kwargs.get("offset_date"):
            rows = [m for m in rows if m.date > kwargs["offset_date"]]
        rows = sorted(rows, key=lambda m: m.id, reverse=not kwargs["reverse"])
        for message in rows[: kwargs["limit"]]:
            yield message


def test_recent_1500_window_preserves_gap_cursor_and_manual_backfill(store):
    fake = OrderedTelegram()
    today = datetime.now(UTC)
    fake.messages = [
        SimpleNamespace(
            id=i, sender_id=1, date=today - timedelta(days=1), message="Вопрос о ключах"
        )
        for i in range(2, 1602)
    ]
    fake.messages.insert(
        0, SimpleNamespace(id=1, sender_id=1, date=today - timedelta(days=31), message="Старое")
    )
    write_secrets(
        store.root, {"TG_API_ID": "123", "TG_API_HASH": "fixture", "TG_SESSION": "fixture"}
    )
    reader = TelegramReader(store, lambda: "salt", lambda *a: fake)
    reader.add("demo-1", TelegramLink(url="https://t.me/valid_chat"))
    first = reader.sync("demo-1", "https://t.me/valid_chat", limit=1500, recent=True)
    assert first["ingest"]["added"] == 1500 and first["partial"] and first["period_days"] == 30
    assert reader.links("demo-1")[0]["cursor"] == 0  # no gap silently skipped
    assert (
        reader.sync("demo-1", "https://t.me/valid_chat", limit=1500, recent=True)["ingest"]["added"]
        == 0
    )
    assert reader.sync("demo-1", "https://t.me/valid_chat")["ingest"]["added"] == 100
    reader.sync("demo-1", "https://t.me/valid_chat")
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM local_messages").fetchone()[0] == 1600
        assert not db.execute("SELECT 1 FROM local_messages WHERE message_id='1'").fetchone()
    assert 29.99 < (today - fake.options[-1]["offset_date"]).total_seconds() / 86400 < 30.01


def test_model_text_budget_keeps_whole_messages_and_filters_old_history(store):
    save_settings(
        store.root, SettingsUpdate(values={"OPENROUTER_API_KEY": "fixture"}, model="test/extract")
    )
    messages = [
        {
            "id": str(i),
            "author_id": "1",
            "sent_at": datetime.now(UTC) - timedelta(days=i),
            "text": "О сроках передачи. " + ("а" * 6980),
        }
        for i in [1, 2, 3, 40]
    ]
    ingest_chat(
        store,
        ChatImport(
            record_id="demo-1", chat_id="a", title="Чат", access="public", messages=messages
        ),
        "salt",
    )
    batches = []

    def classifier(client, cfg, model, rows, topics):
        batches.append(rows)
        return chat_analysis.Labels(
            messages=[
                chat_analysis.Label(
                    index=i, topic="не_размечено", sentiment=None, quote="О сроках передачи."
                )
                for i in range(len(rows))
            ]
        )

    result = chat_analysis.analyze(
        store,
        "demo-1",
        classifier=classifier,
        run_id="r",
        max_messages=60,
        text_budget=12000,
        newest_first=True,
    )
    assert result["labelled"] == 1 and result["more"] and result["characters"] <= 12000
    assert batches[0][0]["message_id"] == "1"
    assert len(batches[0][0]["text"]) == len(
        messages[0]["text"]
    )  # full message, not a hidden truncation


def test_complete_scan_reads_chat_before_followup_and_keeps_data_on_budget_stop(store, monkeypatch):
    from radar.web.operations import BudgetExceeded

    save(store, OperationsSettings(screen_model_calls=0))
    reader = Reader(store)
    reader.add("demo-1", TelegramLink(url="https://t.me/valid_chat"))
    monkeypatch.setattr(
        workflow,
        "rank",
        lambda store, ids: [
            {"record_id": i, "needs_research": True, "excluded": False} for i in ids
        ],
    )

    def research(*a, **kw):
        assert reader.reads
        raise BudgetExceeded("Общий бюджет исчерпан")

    result = workflow.complete_scan(
        store,
        {"record_ids": ["demo-1"], "status": "completed"},
        "scan",
        telegram=reader,
        researcher=research,
    )
    assert result["status"] == "partial" and result["chat_screening"]["records"][0]["chats"]
    assert result["errors"][0]["stage"] == "research"


def test_removed_manual_link_is_not_rediscovered_automatically(store):
    reader = Reader(store)
    link = TelegramLink(url="https://t.me/manual_chat")
    reader.add("demo-1", link)
    reader.remove("demo-1", link)
    assert chat_workflow.candidates(store, "demo-1")[0]["status"] == "rejected"
    result = screening.screen(store, ["demo-1"], "after-removal", reader)
    assert not result["records"][0]["chats"] and not reader.inspections
