import shutil
from pathlib import Path

import pytest

from radar.web import conversation
from radar.web.configuration import write_secrets
from radar.web.discovery import SearchRequest, query_plan
from radar.web.store import Store


@pytest.fixture
def store(tmp_path):
    shutil.copytree("config", tmp_path / "config")
    store = Store(tmp_path)
    with store.connect() as db:
        db.execute("UPDATE records SET dataset='web' WHERE id='demo-1'")
    write_secrets(tmp_path, {"OPENROUTER_API_KEY": "test-placeholder"})
    return store


class Queue:
    def __init__(self):
        self.calls = []

    def submit(self, kind, ident):
        self.calls.append((kind, ident))


def question(request_id="request-001", search=False):
    return conversation.Question(
        question="Почему перенесли сроки?", request_id=request_id, search=search
    )


def test_idempotent_question_and_isolated_persistent_history(store):
    queue = Queue()
    first = conversation.submit(store, queue, "demo-1", question())
    assert conversation.submit(store, queue, "demo-1", question()) == first
    assert len(queue.calls) == 1
    with pytest.raises(ValueError, match="предыдущий"):
        conversation.submit(store, queue, "demo-1", question("request-002"))
    assert conversation.history(Store(store.root), "demo-1")[0]["question"] == question().question
    assert not conversation.history(store, "demo-2")


def test_missing_evidence_returns_explicit_limit_without_search(store):
    conversation.submit(store, Queue(), "demo-1", question())

    def writer(client, cfg, model, context):
        assert context["question"] == question().question
        assert context["name"] == store.get("demo-1")["name"]
        return conversation.Reply(
            sections=[
                conversation.Section(
                    title="Причина не установлена",
                    text="В материалах нет публичного объяснения причины.",
                    kind="limitation",
                )
            ]
        )

    def no_search(*args):
        pytest.fail("Search must be opt-in")

    conversation.answer(store, "demo-1", "run-1", writer, no_search, client=object())
    turn = conversation.history(store, "demo-1")[0]
    assert turn["status"] == "completed"
    assert turn["answer"]["queries"] == []
    assert (
        conversation.answer(store, "demo-1", "retry", writer, no_search, client=object())["status"]
        == "nothing_pending"
    )


def test_unknown_citations_fail_without_fabricated_answer(store):
    conversation.submit(store, Queue(), "demo-1", question())

    def writer(*args):
        return conversation.Reply(
            sections=[
                conversation.Section(
                    title="Причина",
                    text="Причина задержки сообщена в неизвестном документе.",
                    kind="fact",
                    source_ids=["invented"],
                )
            ]
        )

    with pytest.raises(ValueError, match="неизвестный"):
        conversation.answer(store, "demo-1", "run-1", writer, client=object())
    turn = conversation.history(store, "demo-1")[0]
    assert turn["status"] == "failed" and turn["answer"] is None


def test_search_is_bounded_and_citations_use_saved_source(store):
    write_secrets(store.root, {"YANDEX_SEARCH_API_KEY": "test", "YANDEX_FOLDER_ID": "test"})
    conversation.submit(store, Queue(), "demo-1", question(search=True))
    calls = []

    def searcher(store, query, run, client, reader):
        calls.append(query)
        return [
            {
                "source_id": "source-1",
                "title": "Статус стройки",
                "text": "Работы продолжаются.",
                "url": "https://example.org/a",
                "source_kind": "page",
            }
        ], False

    def writer(client, cfg, model, context):
        return conversation.Reply(
            sections=[
                conversation.Section(
                    title="Позиция компании",
                    text="Источник сообщает, что работы продолжаются.",
                    kind="fact",
                    source_ids=["source-1"],
                )
            ]
        )

    conversation.answer(store, "demo-1", "run-1", writer, searcher, client=object())
    answer = conversation.history(store, "demo-1")[0]["answer"]
    assert len(calls) == 2
    assert answer["sources"][0]["url"] == "https://example.org/a"
    assert answer["source_count"] == 1
    assert len(answer["queries"]) == 2
    conversation.submit(store, Queue(), "demo-1", question("request-002", search=False))
    conversation.answer(store, "demo-1", "run-2", writer, searcher, client=object())
    followup = conversation.history(store, "demo-1")[-1]["answer"]
    assert followup["source_count"] == 1
    assert len(calls) == 2  # Follow-up retains the evidence without another search.


def test_expansion_searches_new_directions_without_repeating_base_queries():
    plans = [
        query_plan(Path("."), SearchRequest(region="moscow", expansion=i))[1] for i in range(3)
    ]
    assert all(set(plans[i]).isdisjoint(plans[j]) for i in range(3) for j in range(i + 1, 3))
    assert len(plans[1]) == 4 and len(plans[2]) == 6
    assert any("форум" in q for q in plans[1])
    assert any("наш.дом.рф" in q for q in plans[2])


def test_source_bound_narrative_rejects_invented_reference():
    from radar.web.author import Authored, GroundedParagraph, SituationAnalysis, validate

    paragraph = GroundedParagraph(
        text="Это вывод, который должен иметь источник.", source_ids=["fake"]
    )
    draft = Authored.model_construct(products=[], analysis=SituationAnalysis(overview=paragraph))
    with pytest.raises(ValueError, match="неизвестный источник"):
        validate(draft, {"report": {"products": []}}, [], [])


def test_failed_queue_submission_does_not_leave_pending_question(store):
    class Busy:
        def submit(self, *args):
            raise ValueError("Другая операция выполняется")

    with pytest.raises(ValueError, match="Другая операция"):
        conversation.submit(store, Busy(), "demo-1", question())
    assert conversation.history(store, "demo-1")[0]["status"] == "failed"
    conversation.submit(store, Queue(), "demo-1", question("request-002"))
    assert conversation.history(store, "demo-1")[-1]["status"] == "pending"
