import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from radar.web.chats import ChatImport, aggregates, ingest_chat
from radar.web.sources import ArticleParser
from radar.web.store import Store


@pytest.mark.parametrize(
    "prefix,expected",
    [
        (
            '<script type="application/ld+json">{"@type":"NewsArticle",'
            '"datePublished":"2026-11-01T13:00:00Z"}</script>',
            "2026-11-01",
        ),
        ('<script type="application/ld+json">{"dateModified":"2026-11-01"}</script>', None),
        ('<script type="application/ld+json">{not json}</script>', None),
        ('<article><time datetime="2026-11-02">Вчера</time></article>', "2026-11-02"),
        ('<footer><time datetime="2026-11-02">Дата сайта</time></footer>', None),
    ],
)
def test_source_publication_date_is_not_check_or_modified_date(prefix, expected):
    parser = ArticleParser()
    parser.feed(prefix + "<article>Сроки перенесены</article>")
    assert parser.published == expected
    assert "datePublished" not in "".join(parser.text)
    assert "not json" not in "".join(parser.text)


def test_report_chat_sources_cover_all_messages_not_only_six_examples(tmp_path):
    shutil.copytree(Path("config"), tmp_path / "config")
    store = Store(tmp_path)
    now = datetime.now(UTC)
    messages = [
        {
            "id": str(i),
            "author_id": f"private-{i % 15}",
            "author_name": "Иван Петров",
            "sent_at": (now - timedelta(days=i % 25)).isoformat(),
            "text": (
                "Иван Петров: собираем подписи под коллективным обращением"
                if i == 110
                else f"Вопрос о сроках, сообщение {i}"
            ),
            "topic": f"тема_{i % 8}",
            "sentiment": -1,
        }
        for i in range(120)
    ]
    batch = ChatImport.model_validate(
        {
            "record_id": "demo-1",
            "chat_id": "public-chat",
            "title": "Чат ЖК",
            "access": "public",
            "messages": messages,
        }
    )
    ingest_chat(store, batch, "test-salt")
    result = aggregates(store, "demo-1")
    assert len(result["examples"]) == 6
    assert result["messages_30d"] == 120
    source = result["report_sources"][0]
    summary = json.loads(source["text"])
    assert source["count"] == summary["messages_30d"] == 120
    assert sum(t["messages"] for t in summary["topics"].values()) == 120
    assert len(summary["topics"]) == 8
    assert all(t["weeks"] for t in summary["topics"].values())
    assert any("коллективным обращением" in q["text"] for q in summary["quotes"])
    assert "private-" not in json.dumps(result)
    assert "Иван Петров" not in source["text"]
    assert "author_hash" not in source["text"]
