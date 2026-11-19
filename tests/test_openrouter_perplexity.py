import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from radar.web.perplexity_search import search_perplexity_via_openrouter

CONFIG = {"OPENROUTER_API_KEY": "fixture-gateway-token"}
MODEL = "test/search-model"
QUERY = '"ЖК Заречье" перенос срока сдачи'


@pytest.mark.parametrize(("period", "days"), [("fortnight", 14), ("month", 30), ("all", None)])
def test_openrouter_perplexity_uses_authenticated_search_and_only_structured_sources(period, days):
    def respond(request):
        assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer fixture-gateway-token"
        payload = json.loads(request.content)
        assert payload["model"] == MODEL
        assert payload["web_search_options"] == {"search_context_size": "low"}
        assert 0 < payload["max_tokens"] <= 100
        user_content = payload["messages"][-1]["content"]
        assert user_content.splitlines()[0] == QUERY
        if days is None:
            assert user_content == QUERY
        else:
            cutoff = datetime.now(UTC).date() - timedelta(days=days)
            assert cutoff.isoformat() in user_content
        return httpx.Response(200, json={"choices": [{"message": {
            "content": "Сгенерированный ответ https://fabricated.example/fact",
            "annotations": [
                {"type": "url_citation", "url_citation": {
                    "url": "https://news.example/project?utm_source=search", "title": "Публикация",
                    "content": "Обсуждение сроков проекта.",
                }},
                {"type": "url_citation", "url_citation": {
                    "url": "https://news.example/project", "title": "Повтор",
                }},
                {"type": "url_citation", "url_citation": {"url": "file:///tmp/invalid"}},
                {"type": "malformed", "url_citation": {"url": "https://ignored.example/"}},
            ],
        }}]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert search_perplexity_via_openrouter(client, CONFIG, QUERY, period, model=MODEL) == [
            {
                "url": "https://news.example/project", "title": "Публикация",
                "text": "Обсуждение сроков проекта.", "query": QUERY,
            },
        ]


def test_openrouter_perplexity_handles_provider_metadata_and_url_only_citations():
    payload = {
        "choices": [{"message": {"content": "Непроверенное утверждение."}}],
        "search_results": [
            {"url": "https://article.example/story", "title": "Заголовок", "snippet": "Отрывок."},
            {"url": {"not": "a URL"}},
            *[{"url": f"https://other.example/{index}"} for index in range(7)],
        ],
        "citations": ["https://article.example/story", "https://second.example/story",
                      "https://third.example/story", "https://over-limit.example/story"],
    }
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    with httpx.Client(transport=transport) as client:
        rows = search_perplexity_via_openrouter(client, CONFIG, QUERY, "all", model=MODEL)

    assert len(rows) == 10
    assert rows[0] == {
        "url": "https://article.example/story", "title": "Заголовок", "text": "Отрывок.",
        "query": QUERY,
    }
    assert rows[-2:] == [
        {"url": "https://second.example/story", "title": "", "text": "", "query": QUERY},
        {"url": "https://third.example/story", "title": "", "text": "", "query": QUERY},
    ]


def test_openrouter_perplexity_does_not_extract_results_from_answer_text():
    payload = {"choices": [{"message": {"content": '{"url":"https://fabricated.example/"}'}}]}
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    with httpx.Client(transport=transport) as client:
        assert search_perplexity_via_openrouter(client, CONFIG, QUERY, "all", model=MODEL) == []


def test_openrouter_perplexity_rejects_embedded_errors():
    payload = {"choices": [{"finish_reason": "error", "error": {"message": "upstream detail"}}]}
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    with httpx.Client(transport=transport) as client:
        with pytest.raises(ValueError, match="не завершил поиск"):
            search_perplexity_via_openrouter(client, CONFIG, QUERY, "all", model=MODEL)
