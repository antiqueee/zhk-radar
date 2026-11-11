import json
import shutil
from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
import pytest

from radar.web import evaluation, extra_sources
from radar.web.configuration import write_secrets
from radar.web.operations import usage
from radar.web.store import Store


@pytest.fixture
def store(tmp_path):
    shutil.copytree("config", tmp_path / "config")
    value = Store(tmp_path)
    with value.connect() as db:
        db.execute("UPDATE records SET dataset='web' WHERE id='demo-1'")
    return value


def test_hh_exact_employer_and_truncated_results(store):
    extra_sources.bind(store, "demo-1", extra_sources.Binding(hh_employer="11", confirmed=True))
    write_secrets(store.root, {"HH_USER_AGENT": "Fixture test@example.org"})
    calls = []

    def handle(request):
        calls.append(request)
        assert request.url.params["employer_id"] == "11"
        assert "Fixture" in request.headers["user-agent"]
        return httpx.Response(
            200,
            json={
                "pages": 2,
                "items": [
                    {
                        "id": "101",
                        "name": "Работа с жителями",
                        "employer": {"id": "11"},
                        "snippet": {"responsibility": "<b>Ответы жителям</b>"},
                    },
                    {"id": "102", "name": "Другой работодатель", "employer": {"id": "12"}},
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        first = extra_sources.collect(store, "demo-1", "r", client=client)
        second = extra_sources.collect(store, "demo-1", "r", client=client)
    assert len(calls) == 1 and first["sources"]["hh"]["items"] == second["sources"]["hh"]["items"]
    assert first["status"] == "partial"
    assert first["sources"]["hh"]["items"][0]["text"].strip() == "Ответы жителям"
    assert len(first["sources"]["hh"]["items"]) == 1
    assert usage(store)["calls"] == 1


def test_vk_public_only_posts_comments_no_personal_ids(store):
    extra_sources.bind(store, "demo-1", extra_sources.Binding(vk_group="club12", confirmed=True))
    write_secrets(store.root, {"VK_SERVICE_TOKEN": "fixture-secret"})
    calls = []

    def handle(request):
        params = parse_qs(request.content.decode())
        assert params["access_token"] == ["fixture-secret"]
        assert "fixture-secret" not in str(request.url)
        method = request.url.path.rsplit("/", 1)[-1]
        calls.append(method)
        values = {
            "groups.getById": {"groups": [{"id": 12, "name": "Учебная группа", "is_closed": 0}]},
            "wall.get": {
                "count": 1,
                "items": [
                    {
                        "id": 2,
                        "owner_id": -12,
                        "from_id": 777,
                        "text": "Обсуждение сроков",
                        "date": int(datetime.now(UTC).timestamp()),
                        "comments": {"count": 1},
                    }
                ],
            },
            "wall.getComments": {
                "count": 1,
                "items": [
                    {
                        "id": 3,
                        "from_id": 888,
                        "text": "Вопрос о сроках, мой телефон +7 999 123-45-67",
                        "date": int(datetime.now(UTC).timestamp()),
                    }
                ],
            },
        }
        return httpx.Response(200, json={"response": values[method]})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        value = extra_sources.collect(store, "demo-1", "r", client=client)
        extra_sources.collect(store, "demo-1", "r", client=client)
    assert len(calls) == 3
    assert usage(store)["calls"] == 3
    assert len(value["sources"]["vk"]["items"]) == 2
    assert "from_id" not in json.dumps(value) and "999" not in json.dumps(value)


def test_vk_closed_group_never_reads_wall():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"response": {"groups": [{"id": 12, "is_closed": 1}]}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ValueError, match="открытые"):
            extra_sources.vk(
                client, extra_sources.Binding(vk_group="club12"), {"VK_SERVICE_TOKEN": "fixture"}
            )
    assert len(calls) == 1


def test_inn_validation_and_public_registry_not_snippet(store, monkeypatch):
    with pytest.raises(ValueError, match="Контрольные"):
        extra_sources.Binding(inn="1234567890")
    binding = extra_sources.Binding(inn="7707083893", legal_name="Учебное наименование")
    documents = [
        {
            "url": "https://bankrot.fedresurs.ru/one",
            "text": "ИНН 7707083893",
            "source_kind": "snippet",
        },
        {
            "url": "https://bankrot.fedresurs.ru/two",
            "text": "ИНН 177070838930",
            "source_kind": "page",
        },
        {
            "url": "https://bankrot.fedresurs.ru/three",
            "text": "Участник документа ИНН 7707083893",
            "source_kind": "page",
        },
        {
            "url": "https://bankrot.fedresurs.ru.evil.test/four",
            "text": "ИНН 7707083893",
            "source_kind": "page",
        },
    ]
    monkeypatch.setattr(extra_sources, "query_documents", lambda *a, **kw: (documents, False))
    value = extra_sources.fedresurs(store, None, binding, "r")
    assert len(value["items"]) == 1 and value["items"][0]["url"].endswith("/three")
    assert value["partial"] and "статус" in value["note"]


def test_scheduled_refresh_does_not_repeat_fresh_source(store):
    extra_sources.bind(store, "demo-1", extra_sources.Binding(hh_employer="11", confirmed=True))
    calls = []

    def collector(*args):
        calls.append(1)
        return {"items": [], "partial": False, "note": "fixture"}

    extra_sources.collect(store, "demo-1", "r", collectors={"hh": collector})
    extra_sources.collect(store, "demo-1", "r2", collectors={"hh": collector}, scheduled=True)
    assert calls == [1]
    extra_sources.bind(store, "demo-1", extra_sources.Binding(hh_employer="12", confirmed=True))
    assert extra_sources.status(store, "demo-1")["result"] is None


def test_evaluation_rejects_false_winner_and_counts_duplicates():
    raw = evaluation.example()
    raw["runs"][0]["found_ids"] *= 3
    value = evaluation.measure(evaluation.Evaluation.model_validate(raw))
    assert value["candidate"] is None
    assert value["runs"][0]["duplicates"] == 2
    assert value["runs"][0]["precision"] == 1 and value["runs"][0]["recall"] == 0.5
    raw.update(
        synthetic=False,
        reviewed=True,
        reviewed_by="Аналитик",
        expected_ids=[str(i) for i in range(20)],
    )
    raw["runs"] = [
        {"name": "точный", "found_ids": [str(i) for i in range(19)]},
        {"name": "шумный", "found_ids": [str(i) for i in range(30)]},
    ]
    value = evaluation.measure(evaluation.Evaluation.model_validate(raw))
    assert value["candidate"] == "точный" and value["runs"][1]["false_positive"] == 10
    assert value["runs"][0]["precision_interval_95"][0] < 0.9
    raw["expected_ids"].append("0")
    with pytest.raises(ValueError):
        evaluation.Evaluation.model_validate(raw)


def test_evaluation_no_results_undefined_precision_and_real_label_accuracy():
    raw = evaluation.example()
    raw["runs"][0]["found_ids"] = []
    raw["chat_labels"] = [
        {"id": "a", "expected": "жалоба", "predicted": "жалоба"},
        {"id": "b", "expected": "факт", "predicted": "жалоба"},
    ]
    value = evaluation.measure(evaluation.Evaluation.model_validate(raw))
    assert value["runs"][0]["precision"] is None and value["runs"][0]["recall"] == 0
    assert value["chats"]["accuracy"] == 0.5
    raw["reports"] = [
        {"record_id": "one", "accepted": True, "factual_corrections": 0, "adaptation_score": 4}
    ] * 5
    with pytest.raises(ValueError, match="Повторная"):
        evaluation.Evaluation.model_validate(raw)
