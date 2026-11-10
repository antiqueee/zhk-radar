import json
import shutil
import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest

from radar.web.sources import (
    MAX_BYTES,
    Page,
    SourceError,
    canonical_url,
    fetch_page,
    load_page,
    public_addresses,
)
from radar.web.store import Store


@pytest.fixture
def store(tmp_path):
    shutil.copytree(Path("config"), tmp_path / "config")
    return Store(tmp_path)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.org",
        "https://u:p@example.org",
        "http://example.org:8000",
    ],
)
def test_invalid_source_urls(url):
    with pytest.raises(SourceError):
        canonical_url(url)


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1", "224.0.0.1"]
)
def test_private_and_special_dns_answers_blocked(address):
    def resolver(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

    with pytest.raises(SourceError):
        public_addresses("public.example", 443, resolver)


def test_tracking_dedup_keeps_meaningful_query():
    assert (
        canonical_url("https://Example.ORG/story?id=2&utm_source=x#part")
        == "https://example.org/story?id=2"
    )


def test_article_extraction_excludes_script_and_navigation():
    text = "Жители ЖК Северный сообщают о переносе передачи квартир. " * 4
    html = (
        "<html><head><title>Публикация</title>"
        '<meta property="article:published_time" content="2026-11-10T10:30:00Z"></head>'
        f"<body><nav>Реклама</nav><article><h1>ЖК Северный</h1><p>{text}</p>"
        "<script>тайный скрипт</script></article><footer>Телефон редакции</footer></body></html>"
    )
    page = fetch_page(
        "https://example.org/story", lambda url: (200, {"content-type": "text/html"}, html.encode())
    )
    assert page.status == "loaded" and page.published_at == "2026-11-10"
    assert "Жители" in page.text
    assert all(value not in page.text for value in ["Реклама", "скрипт", "редакции"])
    assert len(page.content_hash) == 64


def test_redirect_validated_and_limits():
    def redirect(url):
        return 302, {"location": "file:///etc/passwd"}, b""

    with pytest.raises(SourceError):
        fetch_page("https://example.org", redirect)
    with pytest.raises(SourceError, match="2 МБ"):
        fetch_page(
            "https://example.org",
            lambda url: (200, {"content-type": "text/html"}, b"x" * (MAX_BYTES + 1)),
        )
    with pytest.raises(SourceError, match="не поддерживается"):
        fetch_page(
            "https://example.org/doc",
            lambda url: (200, {"content-type": "application/pdf"}, b"%PDF"),
        )


def test_page_cache_and_error_isolation(store):
    calls = []

    def reader(url):
        calls.append(url)
        raise SourceError("temporary")

    first, hit = load_page(store, "https://example.org/blocked", reader)
    assert first.status == "unavailable" and not hit
    second, hit = load_page(store, "https://example.org/blocked", reader)
    assert not hit and second.id == first.id and len(calls) == 2
    with store.connect() as db:
        assert (
            json.loads(db.execute("SELECT payload FROM source_pages").fetchone()[0])["status"]
            == "unavailable"
        )


def test_same_content_can_be_rechecked_on_a_new_day(store):
    def reader(url):
        return Page(
            url,
            url,
            "loaded",
            text="Сохранённая публикация",
            content_hash="hash",
            fetched_at=datetime.now(UTC).isoformat(),
        )

    first, _ = load_page(store, "https://example.org", reader)
    with store.connect() as db:
        db.execute("UPDATE source_pages SET fetched_at='2000-01-01'")
    second, cached = load_page(store, "https://example.org", reader)
    assert second.id == first.id and not cached
    assert load_page(store, "https://example.org", reader)[1]


def test_advertising_html_change_does_not_change_document_identity(store):
    def reader(url):
        return Page(
            url,
            url,
            "loaded",
            text="Один и тот же текст статьи",
            content_hash="raw-first",
            fetched_at=datetime.now(UTC).isoformat(),
        )

    first, _ = load_page(store, "https://example.org/story", reader)
    with store.connect() as db:
        db.execute("UPDATE source_pages SET fetched_at='2000-01-01'")

    def changed(url):
        page = reader(url)
        page.content_hash = "raw-with-new-advertising"
        return page

    second, _ = load_page(store, "https://example.org/story", changed)
    assert first.id == second.id
