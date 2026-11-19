import shutil
from datetime import UTC, datetime

import pytest

from radar.web import chat_analysis, conversation
from radar.web.chats import ChatImport, ingest_chat
from radar.web.configuration import SettingsUpdate, save_settings, write_secrets
from radar.web.model_gateway import PROXY_KEY
from radar.web.store import Store


class FakeClient:
    def __init__(self):
        self.closed = False

    def __bool__(self):
        # Explicit dependency injection must be respected even for a falsey adapter.
        return False

    def close(self):
        self.closed = True

    def post(self, *args, **kwargs):
        pytest.fail("No external requests in a route/ownership test")


@pytest.fixture
def store(tmp_path, monkeypatch):
    shutil.copytree("config", tmp_path / "config")
    save_settings(
        tmp_path, SettingsUpdate(values={"OPENROUTER_API_KEY": "fixture"}, model="test/extract")
    )
    write_secrets(tmp_path, {PROXY_KEY: "http://127.0.0.1:10890"})
    result = Store(tmp_path)
    with result.connect() as db:
        db.execute("UPDATE records SET dataset='web' WHERE id='demo-1'")
    monkeypatch.setattr(chat_analysis, "reserve_call", lambda *args: None)
    monkeypatch.setattr("radar.web.workflow.reserve_call", lambda *args: None)
    return result


@pytest.mark.parametrize("injected", [False, True])
def test_chat_labels_default_route_receives_config_and_preserves_client_ownership(
    store, monkeypatch, injected
):
    ingest_chat(
        store,
        ChatImport(
            record_id="demo-1",
            chat_id="public-chat",
            title="Чат",
            access="public",
            messages=[
                {
                    "id": "1",
                    "author_id": "fixture",
                    "sent_at": datetime.now(UTC),
                    "text": "Сроки передачи неизвестны.",
                }
            ],
        ),
        "fixture-salt",
    )
    client, factories = FakeClient(), []

    def factory(cfg, **options):
        assert not injected, "Injected client was incorrectly replaced"
        factories.append((cfg[PROXY_KEY], options["timeout"]))
        return client

    monkeypatch.setattr(chat_analysis, "routed_client", factory)

    def classifier(actual, cfg, model, messages, topics):
        assert actual is client and cfg[PROXY_KEY] == "http://127.0.0.1:10890"
        return chat_analysis.Labels(
            messages=[
                chat_analysis.Label(
                    index=0, topic="не_размечено", sentiment=None, quote=messages[0]["text"]
                )
            ]
        )

    value = chat_analysis.analyze(
        store, "demo-1", classifier=classifier, client=client if injected else None
    )
    assert value["labelled"] == 1 and client.closed is not injected
    assert len(factories) == (0 if injected else 1)
    if not injected:
        assert factories[0][0] == "http://127.0.0.1:10890"
        assert factories[0][1].connect == 15 and factories[0][1].read == 90


@pytest.mark.parametrize("injected", [False, True])
def test_object_dialogue_default_route_receives_config_and_preserves_client_ownership(
    store, monkeypatch, injected
):
    class Queue:
        def submit(self, *args):
            pass

    conversation.submit(
        store,
        Queue(),
        "demo-1",
        conversation.Question(question="Что известно о сроках?", request_id="route-test-001"),
    )
    client, factories = FakeClient(), []

    def factory(cfg, **options):
        assert not injected, "Injected client was incorrectly replaced"
        factories.append((cfg[PROXY_KEY], options["timeout"]))
        return client

    monkeypatch.setattr(conversation, "routed_client", factory)

    def writer(actual, cfg, model, context):
        assert actual is client and cfg[PROXY_KEY] == "http://127.0.0.1:10890"
        return conversation.Reply(
            sections=[
                conversation.Section(
                    title="Материалы",
                    text="Причина переноса в материалах не установлена.",
                    kind="limitation",
                )
            ]
        )

    value = conversation.answer(
        store, "demo-1", "dialogue-route", writer=writer, client=client if injected else None
    )
    assert value["status"] == "completed" and client.closed is not injected
    assert factories == ([] if injected else [("http://127.0.0.1:10890", 120)])
