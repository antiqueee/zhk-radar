from dataclasses import asdict

import pytest

from radar.web.public_communities import (
    is_public_preview,
    public_preview_messages,
    public_preview_url,
)
from radar.web.sources import Page, SourceError, fetch_page


@pytest.mark.parametrize("title", ["Чат ЖК Северный", "Канал покупателей", "Северный — Telegram"])
def test_only_explicit_public_community_search_hits_get_preview(title):
    assert public_preview_url("https://t.me/severny_chat", title) == "https://t.me/s/severny_chat"


@pytest.mark.parametrize(
    "url,title",
    [
        ("https://t.me/+privateinvite", "Чат ЖК"),
        ("https://t.me/joinchat/AAAA123", "Чат ЖК"),
        ("https://t.me/severny_bot", "Telegram бот"),
        ("https://t.me/ivan_petrov", "Telegram: Contact @ivan_petrov"),
        ("https://t.me/severny_chat", "ЖК Северный"),
        ("https://t.me/severny_chat?start=private", "Telegram"),
        ("https://t.me/share/url?url=example.org", "Чат ЖК"),
        ("https://user:password@t.me/severny_chat", "Чат ЖК"),
        ("https://t.me:8080/severny_chat", "Чат ЖК"),
    ],
)
def test_invites_profiles_bots_and_actions_are_excluded(url, title):
    assert public_preview_url(url, title) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://t.me/s/severny_chat?before=11",
        "https://t.me/severny_chat/123",
        "https://t.me.evil.example/severny_chat",
        "https://example.org/t.me/severny_chat",
    ],
)
def test_existing_public_post_urls_and_non_telegram_hosts_are_not_rewritten(url):
    title = "Telegram"
    assert public_preview_url(url, title) == url


def preview_html():
    return """
    <title>Публичный канал ЖК Северный</title>
    <meta property="article:published_time" content="2020-01-01">
    <aside>Описание канала. Телефон автора +7 999 123 45 67</aside>
    <main><time datetime="2021-01-01T01:00:00+00:00">Дата в навигации</time>
    <div class="tgme_widget_message js-widget_message" data-post="severny_chat/11">
      <a class="tgme_widget_message_owner_name">Имя автора</a>
      <div class="tgme_widget_message_text js-message_text" dir="auto">
      Срок перенесён.<br>Ждём встречу &amp; график.</div>
      <a class="tgme_widget_message_date">
      <time datetime="2026-11-15T10:00:00+00:00">15 ноября</time></a>
    </div>
    <div class="tgme_widget_message" data-post="severny_chat/12">
      <div class="tgme_widget_message_text">Встреча состоится 20 ноября.
      <script>Скрытый скрипт</script></div>
      <time datetime="2026-11-16T08:30:00+00:00">16 ноября</time>
    </div>
    <div class="tgme_widget_message" data-post="unrelated_channel/99">
      <div class="tgme_widget_message_text">Чужой канал</div>
      <time datetime="2026-11-16T09:00:00Z"></time>
    </div></main><footer>Download Telegram</footer>
    """


def test_public_post_dates_stay_with_exact_bodies_and_no_person_profile_collection():
    messages, text = public_preview_messages("https://t.me/s/severny_chat", preview_html())
    assert messages == [
        {
            "url": "https://t.me/severny_chat/11",
            "date": "2026-11-15T10:00:00+00:00",
            "text": "Срок перенесён.\nЖдём встречу & график.",
        },
        {
            "url": "https://t.me/severny_chat/12",
            "date": "2026-11-16T08:30:00+00:00",
            "text": "Встреча состоится 20 ноября.",
        },
    ]
    assert (
        text.index(messages[0]["date"])
        < text.index(messages[0]["text"])
        < text.index(messages[1]["date"])
    )
    assert all(
        value not in text
        for value in ["Имя автора", "Телефон автора", "Чужой канал", "Скрытый скрипт", "2021-01-01"]
    )
    assert public_preview_messages("https://example.org/s/severny_chat", preview_html()) == ([], "")


def test_preview_is_not_dated_as_a_whole_channel_and_metadata_survives_cache():
    page = fetch_page(
        "https://t.me/s/severny_chat",
        lambda url: (200, {"content-type": "text/html"}, preview_html().encode()),
    )
    assert page.status == "loaded" and page.published_at is None
    assert len(page.messages) == 2
    assert page.links == [message["url"] for message in page.messages]
    assert Page(**asdict(page)).messages == page.messages


def test_empty_or_login_only_preview_is_not_read_chat_content():
    html = (
        "<title>Telegram</title><div>Sign in to Telegram. Join this group. "
        + "Описание. " * 30
        + "</div>"
    )
    with pytest.raises(SourceError, match="сообщения"):
        fetch_page(
            "https://t.me/s/severny_chat",
            lambda url: (200, {"content-type": "text/html"}, html.encode()),
        )


def test_message_budget_never_truncates_a_quote_or_invents_a_date():
    html = (
        '<div class="tgme_widget_message" data-post="severny_chat/11">'
        '<div class="tgme_widget_message_text">Текст без даты.</div>'
        '<time datetime="ошибка"></time></div>'
    )
    messages, text = public_preview_messages("https://t.me/s/severny_chat", html)
    assert messages[0]["date"] is None and "Дата сообщения" not in text
    assert public_preview_messages("https://t.me/s/severny_chat", html, max_text=10) == ([], "")
    assert is_public_preview("https://t.me/s/severny_chat/11")
    assert not is_public_preview("https://t.me/severny_chat/11")
