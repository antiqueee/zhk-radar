"""Message-feed contract tests use an in-process HTTP transport."""

import json
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import ValidationError

from radar.web.brand_analytics_transport import (
    FeedSettings,
    TransportError,
    fetch_page,
    read_export,
)

SINCE = datetime(2026, 11, 15, tzinfo=UTC)
UNTIL = datetime(2026, 11, 16, tzinfo=UTC)
ROW = {"id": 24, "url": "https://example.test/one", "text": "Сообщение о ЖК"}


def settings(**overrides):
    return FeedSettings.from_mapping({
        "BRAND_ANALYTICS_ENDPOINT": "https://api.brandanalytics.ru/messages",
        "BRAND_ANALYTICS_TOKEN": "unit-test-key",
        "BRAND_ANALYTICS_TOPIC_IDS": "main,secondary",
        **{f"BRAND_ANALYTICS_{key.upper()}": value for key, value in overrides.items()},
    })


def test_feed_maps_nested_messages_with_fixed_pagination_and_auth():
    config = settings(
        endpoint="https://api.br-analytics.ru/topic/{topic_id}/messages",
        method="POST",
        token_header="X-API-Key",
        token_scheme="",
        request_fields=json.dumps({
            "topic_id": None, "since": "from", "until": "to", "cursor": "next", "limit": "count",
        }),
        static_params='{"type": "posts"}',
        messages_path="result.rows",
        next_cursor_path="result.cursor",
        field_map=json.dumps({
            "provider_id": "id", "url": "link.canonical", "text": "message.plain",
            "published_at": "posted", "truncated": "isSnippet",
        }),
        date_format="unix",
        page_size="2",
    )
    seen = []

    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/topic/main/messages"
        assert request.headers["X-API-Key"] == "unit-test-key"
        body = json.loads(request.content)
        assert body["from"] == int(SINCE.timestamp())
        assert body["to"] == int(UNTIL.timestamp())
        assert body["count"] == 2 and body["type"] == "posts"
        seen.append(body.get("next"))
        cursor = None if body.get("next") else 17
        return httpx.Response(200, json={"result": {"rows": [{
            "id": 45,
            "link": {"canonical": "https://example.test/two"},
            "message": {"plain": "Текст"},
            "posted": "15.11.2026 15:10:21",
            "isSnippet": True,
        }], "cursor": cursor}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        page = fetch_page(config, topic_id="main", since=SINCE, until=UNTIL, client=client)
        assert page.next_cursor == "17"
        assert page.raw_count == 1
        assert page.messages == [{
            "provider_id": "45", "url": "https://example.test/two", "text": "Текст",
            "published_at": "2026-11-15T12:10:21+00:00", "truncated": True,
            "source_kind": "snippet",
        }]
        final = fetch_page(
            config, topic_id="main", since=SINCE, until=UNTIL, cursor=page.next_cursor,
            client=client,
        )
    assert final.next_cursor is None
    assert seen == [None, "17"]


@pytest.mark.parametrize("status,retryable", [(429, True), (403, False), (302, False)])
def test_feed_safe_http_failure_never_follows_redirects_or_echoes_body(status, retryable):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        assert request.headers["Authorization"] == "Bearer unit-test-key"
        assert request.url.params["topic_id"] == "main"
        assert request.url.params["since"] == SINCE.isoformat()
        return httpx.Response(
            status, content=b"unit-test-key private body",
            headers={"Retry-After": "13", "Location": "https://elsewhere.test/leak"},
        )

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        with pytest.raises(TransportError) as caught:
            fetch_page(settings(), topic_id="main", since=SINCE, client=client)
    assert count == 1
    assert caught.value.status_code == status and caught.value.retryable is retryable
    assert caught.value.retry_after == 13
    assert "unit-test-key" not in str(caught.value) and "private body" not in str(caught.value)


@pytest.mark.parametrize("overrides", [
    {"endpoint": "http://api.brandanalytics.ru/messages"},
    {"endpoint": "https://api.brandanalytics.ru.evil.test/messages"},
    {"endpoint": "https://evil.test@api.brandanalytics.ru/messages"},
    {"endpoint": "https://api.brandanalytics.ru/messages?access_token=secret"},
    {"endpoint": "https://api.brandanalytics.ru:8042/messages"},
    {"token_header": "Host"},
    {"token": "unit-test-key\nHeader: hidden"},
    {"topic_ids": "one,two,three"},
    {"request_fields": '{"since": null}'},
    {"request_fields": '{"since": "topic_id"}'},
])
def test_rejects_unsafe_endpoint_or_nonincremental_profile(overrides):
    with pytest.raises(ValidationError) as caught:
        settings(**overrides)
    assert "unit-test-key" not in str(caught.value) and "access_token" not in str(caught.value)


@pytest.mark.parametrize("payload,overrides,cursor", [
    ({"messages": []}, {"messages_path": "other"}, None),
    ({"data": {"counts": 10}}, {}, None),
    ({"messages": [ROW], "has_more": True}, {}, None),
    ({"messages": [ROW], "next_cursor": "same"}, {}, "same"),
    ({"messages": [ROW]}, {"page_size": "1"}, None),
])
def test_incomplete_pages_do_not_look_complete(payload, overrides, cursor):
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    with httpx.Client(transport=transport) as client:
        with pytest.raises(TransportError):
            fetch_page(
                settings(**overrides), topic_id="main", since=SINCE, cursor=cursor, client=client,
            )


def test_export_accepts_russian_csv_and_nested_json_with_utc_dates():
    raw = (
        "ID сообщения;Ссылка на сообщение;Текст сообщения;Дата публикации\r\n"
        '42;https://example.test/one;"Первая строка\nвторая; строчка";15.11.2026 17:00\r\n'
    )
    assert read_export(raw.encode("cp1251")) == [{
        "provider_id": "42",
        "url": "https://example.test/one",
        "text": "Первая строка\nвторая; строчка",
        "published_at": "2026-11-15T14:00:00+00:00",
    }]
    nested = json.dumps({"collection": {"rows": [
        {**ROW, "published_at": "2026-11"},
        {**ROW, "published_at": "2026-11-15T17:00:00+03:00"},
    ]}}).encode()
    assert [message.get("published_at") for message in read_export(
        nested, messages_path="collection.rows",
    )] == [None, "2026-11-15T14:00:00+00:00"]


def test_snippet_export_infers_discovery_only_and_keeps_vendor_hash():
    raw = "Hash сообщения;Ссылка на публикацию;Сниппет\nabc;https://example.test/p;отрывок\n"
    assert read_export(raw.encode()) == [{
        "provider_id": "abc", "url": "https://example.test/p", "text": "отрывок",
        "truncated": True, "source_kind": "snippet",
    }]


def test_large_and_timed_out_responses_have_distinct_safe_errors(monkeypatch):
    monkeypatch.setattr("radar.web.brand_analytics_transport._MAX_PAGE_BYTES", 10)

    def fail(request):
        raise httpx.ReadTimeout("unit-test-key raw upstream error", request=request)

    cases = (
        (lambda _: httpx.Response(200, content=b"0" * 12), False),
        (fail, True),
    )
    for handler, retryable in cases:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(TransportError) as caught:
                fetch_page(settings(), topic_id="main", since=SINCE, client=client)
        assert caught.value.retryable is retryable
        assert "unit-test-key" not in str(caught.value)
