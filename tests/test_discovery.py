import base64
import json
import shutil
from pathlib import Path

import httpx
import pytest

from radar.web.configuration import SettingsUpdate, save_settings
from radar.web.discovery import SearchRequest, discover
from radar.web.store import Store


@pytest.fixture
def store(tmp_path):
    shutil.copytree(Path("config"), tmp_path / "config")
    save_settings(
        tmp_path,
        SettingsUpdate(
            values={
                "YANDEX_SEARCH_API_KEY": "test-search",
                "YANDEX_FOLDER_ID": "test-folder",
                "OPENROUTER_API_KEY": "test-model",
            },
            model="test/extractor",
        ),
    )
    return Store(tmp_path)


def result(quote="Жители ЖК Северный сообщают о переносе передачи квартир."):
    return {
        "objects": [
            {
                "name": "ЖК Северный",
                "developer": "Не установлен",
                "region": "Москва",
                "summary": "В публикации жители сообщают о переносе передачи квартир.",
                "source_index": 0,
                "quote": quote,
            }
        ]
    }


def transport(reply=None):
    calls = []

    def handler(request):
        calls.append(request)
        if "yandex" in request.url.host:
            xml = (
                "<yandexsearch><response><results><doc><url>https://example.org/news</url>"
                "<title>ЖК Северный</title><passages><passage>Жители ЖК Северный сообщают "
                "о переносе передачи квартир.</passage></passages></doc></results>"
                "</response></yandexsearch>"
            )
            return httpx.Response(200, json={"rawData": base64.b64encode(xml.encode()).decode()})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(reply or result())}}]}
        )

    return httpx.MockTransport(handler), calls


def test_discovery_cache_provenance_and_dedup(store):
    mock, calls = transport()
    with httpx.Client(transport=mock) as client:
        first = discover(store, SearchRequest(region="capitals"), client)
        assert first["added"] == 1 and first["found"] == 1
        assert first["external_requests"] == 5
        second = discover(store, SearchRequest(region="capitals"), client)
        assert second["added"] == 0 and second["cached"]
        assert len(calls) == 5
    records = store.records("web")
    assert len(records) == 1 and records[0]["assessment"] is None
    assert records[0]["verification"] == "unverified_search_snippet"
    record = store.get(records[0]["id"])
    assert record["evidence"][0]["quote"] == result()["objects"][0]["quote"]
    assert "ЖК Северный" in record["claims"]["Поисковый фрагмент"]


def test_hallucinated_quote_not_imported(store):
    mock, _ = transport(result("Выдуманная цитата без доказательства"))
    with httpx.Client(transport=mock) as client:
        run = discover(store, SearchRequest(region="moscow"), client)
    assert run["discarded"] == 1 and run["added"] == 0
    assert store.records("web") == []


def test_invalid_model_schema_fails_closed(store):
    mock, _ = transport({"text": "unstructured answer"})
    with httpx.Client(transport=mock) as client, pytest.raises(ValueError, match="некорректный"):
        discover(store, SearchRequest(region="moscow"), client)
    assert store.records("web") == []


def test_remote_error_does_not_expose_credentials(store):
    mock = httpx.MockTransport(lambda r: httpx.Response(401, json={"error": "secret test-search"}))
    with httpx.Client(transport=mock) as client, pytest.raises(ValueError) as error:
        discover(store, SearchRequest(region="moscow"), client)
    assert "test-search" not in str(error.value)
    assert "401" in str(error.value)
