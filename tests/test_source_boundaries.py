import pytest

from radar.reports.pipeline_v8 import registry_sources
from radar.web.sources import primary_text

URL = "https://news.mondiara.com/categories/108/posts/265038"
MAIN = "В Nagatino i-Land завершены работы в корпусе 3."
RELATED = "Корпус 4 другого сообщения передадут в следующем году."


@pytest.mark.parametrize("heading", [
    "Другие новости сообщества",
    "Полный профиль компании доступен в MONDIARA",
])
def test_mondiara_primary_article_ends_before_profile_or_related_news(heading):
    text = MAIN + "\n" + heading + "\n" + RELATED
    assert primary_text(URL, text) == MAIN
    assert primary_text(URL + "?utm_source=search", text) == MAIN


@pytest.mark.parametrize("url", [
    "https://example.org/categories/108/posts/265038",
    "https://news.mondiara.com.evil.example/categories/108/posts/265038",
    "https://news.mondiara.com/categories/108",
])
def test_mondiara_boundary_is_not_a_generic_cross_site_filter(url):
    text = MAIN + "\nДругие новости сообщества\n" + RELATED
    assert primary_text(url, text) == text


def test_heading_words_inside_article_are_not_a_section_boundary():
    text = MAIN + '\nАвтор говорит: «Другие новости сообщества важны читателям». '
    assert primary_text(URL, text) == text


def test_chatdoma_project_reviews_end_before_sitewide_comments():
    url = "https://chatdoma.ru/zhk-zareche-park/otzyvy/"
    main = "ЖК «Заречье Парк»\nВсе отзывы\nЖители обсуждают парковку во дворе."
    related = "Перейти в telegram чат\nГрунт у Фонтанки сложный."
    assert primary_text(url, main + "\n" + related) == main


def test_v8_registry_masks_related_news_in_historical_snapshot():
    payload = {"report_research": {"documents": [{
        "url": URL, "title": "Nagatino i-Land", "read_status": "loaded", "source_id": "old-page",
        "source_kind": "page", "published_at": "2026-11-06",
        "text": MAIN + "\nДругие новости сообщества\n" + RELATED,
    }]}}
    sources = registry_sources(payload)
    assert len(sources) == 1
    assert sources[0]["text"] == MAIN
    assert sources[0]["published_at"] == "2026-11-06"
    # The stored snapshot is kept intact; masking affects only the research input.
    assert RELATED in payload["report_research"]["documents"][0]["text"]
