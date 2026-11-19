import json
from datetime import UTC, datetime, timedelta

import pytest

from radar.web import report_chats as chats
from radar.web.chats import ChatImport, Message, ingest_chat
from radar.web.store import Store


class Reader:
    def __init__(self, store, metadata=None, fail=False):
        self.store, self.metadata, self.fail = store, metadata, fail
        self.inspections, self.reads = [], []
        with store.connect() as db:
            db.execute(
                "CREATE TABLE telegram_links(record_id,url,peer_id,title,cursor,synced_at,status)"
            )

    def status(self):
        return {"session_saved": True}

    def links(self, ident):
        with self.store.connect() as db:
            return [
                dict(r)
                for r in db.execute("SELECT * FROM telegram_links WHERE record_id=?", (ident,))
            ]

    def inspect(self, url):
        self.inspections.append(url)
        return {**self.metadata, "url": url}

    def add(self, ident, link):
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO telegram_links VALUES(?,?,?,?,?,?,?)",
                (ident, link.url, None, self.metadata["title"], 0, None, "added"),
            )

    def sync(self, ident, url, *, limit, recent):
        self.reads.append((url, limit, recent))
        if self.fail:
            raise ValueError("Источник временно недоступен")
        with self.store.connect() as db:
            db.execute(
                "UPDATE telegram_links SET synced_at=?,status='synced' WHERE url=?",
                (datetime.now(UTC).isoformat(), url),
            )
        return {"read": 0, "partial": False}


def setup(tmp_path):
    class ReportStore(Store):
        def seed(self):
            pass

    store = ReportStore(tmp_path)
    record = {"id": "test", "name": "Северный берег", "region": "Москва", "urls": [], "aliases": []}
    with store.connect() as db:
        db.execute(
            "INSERT INTO records(id,dataset,payload,updated_at) VALUES(?,?,?,?)",
            ("test", "web", json.dumps(record), datetime.now(UTC).isoformat()),
        )
    return store


def test_existing_public_chat_is_reused_and_evidence_has_real_coverage(tmp_path):
    store = setup(tmp_path)
    reader = Reader(store)
    at = datetime.now(UTC)
    with store.connect() as db:
        db.execute(
            "INSERT INTO telegram_links VALUES(?,?,?,?,?,?,?)",
            (
                "test",
                "https://t.me/bereg_chat",
                "321",
                "Северный берег",
                3,
                at.isoformat(),
                "synced",
            ),
        )
    ingest_chat(
        store,
        ChatImport(
            record_id="test",
            chat_id="321",
            title="Северный берег",
            access="public",
            messages=[
                Message(
                    id=str(i), author_id="private-author", sent_at=at - timedelta(days=1), text=text
                )
                for i, text in enumerate(
                    ["Когда ключи? Срок снова перенесли.", "Напор воды слабый."]
                )
            ],
        ),
        "salt",
    )
    value = chats.ensure_public_chats(store, "test", telegram=reader, at=at)
    assert reader.reads == [] and reader.inspections == []
    assert value["read"][0]["cached"]
    doc = value["documents"][0]
    assert doc["coverage"] == {
        "read": 2,
        "selected_for_facts": 2,
        "partial": False,
        "labelled": False,
    }
    assert "Когда ключи?" in doc["text"]
    assert "private-author" not in doc["text"] and "negative_share" not in doc


def test_discovered_group_requires_project_region_and_pins_real_peer(tmp_path):
    store = setup(tmp_path)
    reader = Reader(
        store,
        {
            "title": "ЖК Северный берег Москва",
            "description": "Чат дольщиков",
            "public": True,
            "group": True,
            "peer_id": "777",
        },
    )
    research = {"documents": [{"url": "https://t.me/s/bereg_chat"}]}
    value = chats.ensure_public_chats(store, "test", telegram=reader, research=research)
    assert reader.reads == [("https://t.me/bereg_chat", 1000, True)]
    assert reader.links("test")[0]["peer_id"] == "777"
    assert value["documents"] == []  # An empty read is never presented as messages or sentiment.
    assert value["status"] == "no_messages"


def test_wrong_region_private_invites_and_rejected_chat_are_not_read(tmp_path):
    store = setup(tmp_path)
    reader = Reader(
        store,
        {
            "title": "ЖК Северный берег Казань",
            "description": "Чат дольщиков",
            "public": True,
            "group": True,
            "peer_id": "777",
        },
    )
    with store.connect() as db:
        db.execute("CREATE TABLE chat_candidates(record_id,url,payload,status)")
        db.execute(
            "INSERT INTO chat_candidates VALUES(?,?,?,?)",
            ("test", "https://t.me/rejected_chat", "{}", "rejected"),
        )
    value = chats.ensure_public_chats(
        store,
        "test",
        telegram=reader,
        research={
            "documents": [
                {"url": "https://t.me/+private_invite"},
                {"url": "https://t.me/rejected_chat"},
                {"url": "https://t.me/wrongregion_chat"},
            ]
        },
    )
    assert reader.inspections == ["https://t.me/wrongregion_chat"]
    assert reader.reads == [] and value["skipped"]


def test_unavailable_reader_preserves_error_and_never_invents_chat_coverage(tmp_path):
    store = setup(tmp_path)
    reader = Reader(store, fail=True)
    with store.connect() as db:
        db.execute(
            "INSERT INTO telegram_links VALUES(?,?,?,?,?,?,?)",
            ("test", "https://t.me/bereg_chat", "321", "Северный берег", 3, None, "added"),
        )
    value = chats.ensure_public_chats(store, "test", telegram=reader)
    assert value["status"] == "unavailable" and value["errors"]
    assert value["documents"] == []


def put_link(store, *, url="https://t.me/bereg_chat", peer="321", at=None):
    with store.connect() as db:
        db.execute(
            "INSERT INTO telegram_links VALUES(?,?,?,?,?,?,?)",
            ("test", url, peer, "Северный берег", 0, at, "synced" if at else "added"),
        )


def put_messages(store, peer, messages):
    from radar.web.chats import initialize

    initialize(store)
    with store.connect() as db:
        for ident, stamp, text in messages:
            db.execute(
                "INSERT INTO local_messages VALUES(?,?,?,?,?,?,?,?)",
                ("test", peer, ident, "private-author", stamp, text, "не_размечено", None),
            )


def metadata(**changes):
    return {
        "title": "ЖК Северный берег Москва",
        "description": "Чат дольщиков",
        "public": True,
        "group": True,
        "peer_id": "777",
        **changes,
    }


def test_cached_document_uses_actual_read_time_and_stays_stable_inside_ttl(tmp_path):
    store = setup(tmp_path)
    reader = Reader(store)
    at = datetime.now(UTC)
    put_link(store, at=at.isoformat())
    # Near the moving 30-day boundary: reusing the same snapshot must preserve it.
    put_messages(
        store,
        "321",
        [("1", (at - timedelta(days=30) + timedelta(minutes=30)).isoformat(), "Когда ключи?")],
    )
    first = chats.ensure_public_chats(store, "test", telegram=reader, at=at)
    later = chats.ensure_public_chats(store, "test", telegram=reader, at=at + timedelta(hours=1))
    assert first["documents"] == later["documents"]
    assert later["documents"][0]["checked_at"] == at.isoformat()
    assert reader.reads == [] and later["read"][0]["cached"] is True


@pytest.mark.parametrize("stamp", ["naive", "future"])
def test_naive_or_future_sync_time_is_not_reused(tmp_path, stamp):
    store = setup(tmp_path)
    reader = Reader(store)
    at = datetime.now(UTC)
    value = (
        at.replace(tzinfo=None).isoformat()
        if stamp == "naive"
        else (at + timedelta(days=1)).isoformat()
    )
    put_link(store, at=value)
    result = chats.ensure_public_chats(store, "test", telegram=reader, at=at)
    assert len(reader.reads) == 1
    assert result["read"][0]["cached"] is False


def test_future_and_naive_messages_never_enter_evidence(tmp_path):
    store = setup(tmp_path)
    reader = Reader(store)
    at = datetime.now(UTC)
    put_link(store, at=at.isoformat())
    put_messages(
        store,
        "321",
        [
            ("1", (at - timedelta(days=1)).isoformat(), "Когда передача ключей?"),
            ("2", (at + timedelta(days=1)).isoformat(), "FUTURE"),
            ("3", at.replace(tzinfo=None).isoformat(), "NAIVE"),
        ],
    )
    result = chats.ensure_public_chats(store, "test", telegram=reader, at=at)
    assert result["documents"][0]["count"] == 1
    assert result["selection"][0]["excluded_invalid_timestamps"] == 1
    assert result["selection"][0]["excluded_future"] == 1
    assert "FUTURE" not in result["documents"][0]["text"]
    assert "NAIVE" not in result["documents"][0]["text"]


def test_one_global_model_budget_preserves_whole_messages_and_counts_full_window(
    tmp_path, monkeypatch
):
    store = setup(tmp_path)
    reader = Reader(store)
    at = datetime.now(UTC)
    monkeypatch.setattr(chats, "MODEL_CHAR_BUDGET", 1800)
    for peer, url in [("321", "https://t.me/bereg_chat"), ("322", "https://t.me/bereg_other")]:
        put_link(store, peer=peer, url=url, at=at.isoformat())
        put_messages(
            store,
            peer,
            [
                (
                    str(i),
                    (at - timedelta(seconds=i + 1)).isoformat(),
                    "Когда ключи? " + "Важные детали. " * 20,
                )
                for i in range(10)
            ],
        )
    value = chats.ensure_public_chats(store, "test", telegram=reader, at=at)
    assert sum(len(d["text"]) for d in value["documents"]) <= 1800
    assert sum(d["stored_30d"] for d in value["selection"]) == 20
    assert sum(d["selected_for_facts"] for d in value["selection"]) < 20
    assert all(
        m["text"].endswith("Важные детали. ") for d in value["documents"] for m in d["messages"]
    )
    assert value["status"] == "partial"


def test_actual_project_href_can_corroborate_region_but_snippet_or_mention_cannot(tmp_path):
    store = setup(tmp_path)
    reader = Reader(store, metadata(title="ЖК Северный берег"))
    document = {
        "url": "https://builder.example/project",
        "title": "ЖК Северный берег Москва",
        "text": "ЖК Северный берег в Москве. Открытый чат дольщиков.",
        "read_status": "loaded",
        "source_kind": "page",
        "links": ["https://t.me/bereg_chat"],
    }
    value = chats.ensure_public_chats(
        store, "test", telegram=reader, research={"documents": [document]}
    )
    assert reader.reads and value["status"] == "no_messages"

    other_store = setup(tmp_path / "other")
    other = Reader(other_store, metadata(title="ЖК Северный берег"))
    chats.ensure_public_chats(
        other_store,
        "test",
        telegram=other,
        research={
            "documents": [{**document, "source_kind": "snippet", "read_status": "unavailable"}]
        },
    )
    assert other.reads == []

    third_store = setup(tmp_path / "third")
    third = Reader(third_store, metadata(title="ЖК Северный берег"))
    chats.ensure_public_chats(
        third_store,
        "test",
        telegram=third,
        research={
            "documents": [
                {**document, "links": [], "text": document["text"] + " https://t.me/bereg_chat"}
            ]
        },
    )
    assert third.inspections == [] and third.reads == []

    wrong_store = setup(tmp_path / "wrong")
    wrong = Reader(wrong_store, metadata(title="ЖК Северный берег Казань"))
    chats.ensure_public_chats(
        wrong_store, "test", telegram=wrong, research={"documents": [document]}
    )
    assert wrong.reads == []  # Moscow in a page cannot override Kazan in the linked chat title.


def test_alias_cannot_bypass_explicit_phase_mismatch(tmp_path):
    store = setup(tmp_path)
    record = store.get("test")
    with store.connect() as db:
        db.execute(
            "UPDATE records SET payload=? WHERE id='test'",
            (
                json.dumps(
                    {**record, "name": "Северный берег II очередь", "aliases": ["Северный берег"]}
                ),
            ),
        )
    reader = Reader(store, metadata(title="ЖК Северный берег I очередь Москва"))
    chats.ensure_public_chats(
        store, "test", telegram=reader, research={"documents": [{"url": "https://t.me/bereg_chat"}]}
    )
    assert reader.inspections and reader.reads == []


@pytest.mark.parametrize(
    "url",
    [
        "https://t.me/+private",
        "https://t.me/joinchat/private",
        "https://user:pass@t.me/bereg_chat",
        "https://t.me/bereg_chat?start=1",
        "https://t.me/bereg_bot",
        "https://t.me/c/123/4",
        "https://t.me/bereg_chat/not-a-post",
    ],
)
def test_private_action_credentials_and_bot_urls_are_rejected_before_sdk(url):
    assert chats._public_url(url) is None


def test_private_or_person_metadata_never_adds_chat(tmp_path):
    store = setup(tmp_path)
    reader = Reader(store, metadata(public=False, group=False))
    value = chats.ensure_public_chats(
        store,
        "test",
        telegram=reader,
        research={"documents": [{"url": "https://t.me/person_name"}]},
    )
    assert reader.reads == [] and reader.links("test") == []
    assert value["status"] == "no_matching_chats"


def test_inspection_and_chat_limits_are_bounded(tmp_path):
    store = setup(tmp_path)
    reader = Reader(store, metadata(title="Другой объект Казань"))
    research = {"documents": [{"url": f"https://t.me/bereg_group{i}"} for i in range(8)]}
    chats.ensure_public_chats(store, "test", telegram=reader, research=research)
    assert len(reader.inspections) == 3 and reader.reads == []


def test_existing_unpinned_explicit_link_is_pinned_before_read(tmp_path):
    store = setup(tmp_path)
    reader = Reader(store, metadata())
    put_link(store, peer=None)
    chats.ensure_public_chats(store, "test", telegram=reader)
    assert reader.inspections == ["https://t.me/bereg_chat"]
    assert reader.links("test")[0]["peer_id"] == "777"
    assert len(reader.reads) == 1


def test_source_error_does_not_leak_raw_sdk_details(tmp_path):
    store = setup(tmp_path)

    class FailedReader(Reader):
        def sync(self, *args, **kwargs):
            raise ValueError("SECRET session +79991234567")

    reader = FailedReader(store)
    put_link(store)
    value = chats.ensure_public_chats(store, "test", telegram=reader)
    assert value["status"] == "unavailable" and value["errors"]
    assert "SECRET" not in json.dumps(value) and "+7999" not in json.dumps(value)
