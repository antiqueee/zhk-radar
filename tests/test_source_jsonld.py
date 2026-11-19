import json

import pytest

from radar.web.sources import ArticleParser, SourceError, fetch_page

URL = "https://developer.example/press/actual-article"
BODY = (
    "В жилом комплексе продолжается строительство новых очередей. "
    "Застройщик передаёт ключи покупателям готовых домов. "
) * 3


def ld(article):
    value = json.dumps(article).replace("</", "<\\/")
    return '<script type="application/ld+json">' + value + "</script>"


def article(**changes):
    return {
        "@type": "Article",
        "url": URL,
        "headline": "Строительство новых очередей",
        "articleBody": BODY,
        **changes,
    }


def load(html, url=URL):
    def transport(requested):
        return 200, {"content-type": "text/html; charset=utf-8"}, html.encode()

    return fetch_page(url, transport=transport)


def flight(data):
    payload = ["$", "section", None, {"initialState": {"data": data}}]
    transport = json.dumps([1, "1:" + json.dumps(payload) + "\n"])
    return "<script>self.__next_f.push(" + transport + ")</script>"


def test_sparse_spa_extracts_matching_article_body_without_invented_date():
    page = load(
        "<title>Официальная новость</title><main>Обратная связь</main>"
        + ld(article(articleBody="<p>" + BODY + "</p><script>danger()</script>",
                     dateModified="2026-11-08T12:00:00Z"))
    )
    assert page.text == BODY.strip()
    assert "Обратная связь" not in page.text
    assert "danger" not in page.text
    assert page.published_at is None


@pytest.mark.parametrize("kind", ["Article", "NewsArticle", "BlogPosting"])
def test_matching_jsonld_article_types_and_canonical_url(kind):
    page = load(ld(article(
        **{"@type": kind}, url=URL + "?utm_source=test#article",
        datePublished="2026-11-08T00:00:00",
    )))
    assert page.text == BODY.strip()
    assert page.published_at == "2026-11-08"


def test_main_entity_has_priority_and_related_articles_are_not_document_roots():
    primary = article(mainEntityOfPage={"@id": URL}, datePublished="2026-11-08")
    weaker = article(articleBody="Сторонний текст. " * 20, datePublished="2026-01-01")
    # Even an exact URL inside a recommendation must not become the primary article.
    page = load(ld({"@graph": [weaker, {"relatedArticle": primary}, primary]}))
    assert page.text == BODY.strip()
    assert page.published_at == "2026-11-08"


def test_mismatched_article_and_nested_recommendation_cannot_supply_body_or_date():
    wrong = article(url="https://developer.example/press/other", datePublished="2026-11-08")
    parser = ArticleParser(URL)
    parser.feed(ld({"@type": "WebPage", "relatedArticle": article()}) + ld(wrong))
    assert parser.structured_body() == ""
    assert parser.published is None
    with pytest.raises(SourceError, match="Недостаточно текста"):
        load("<main>Обратная связь</main>" + ld(wrong))


def test_conflicting_main_entity_cannot_be_overruled_by_matching_url():
    parser = ArticleParser(URL)
    parser.feed(ld(article(mainEntityOfPage="https://developer.example/other")))
    assert parser.structured_body() == ""


def test_redirect_uses_final_url_for_jsonld_identity():
    def transport(requested):
        if requested != URL:
            return 302, {"location": URL}, b""
        return 200, {"content-type": "text/html"}, ld(article()).encode()

    page = fetch_page("https://developer.example/old", transport=transport)
    assert page.final_url == URL
    assert page.text == BODY.strip()


def test_normal_visible_article_remains_primary():
    visible = "Официальный текст из основного блока страницы. " * 8
    page = load("<article>" + visible + "</article>" + ld(article()))
    assert page.text == visible.strip()


def test_first_party_flight_date_requires_exact_headline_and_slug():
    data = {
        "header": "Строительство новых очередей",
        "link": "actual-article",
        "dateBegin": "2026-11-08T00:00:00",
    }
    page = load(flight(data) + ld(article()))
    assert page.published_at == "2026-11-08"


@pytest.mark.parametrize("changes", [
    {"header": "Совсем другая новость"},
    {"link": "other-article"},
    {"dateBegin": None, "dateModified": "2026-11-08"},
    {"dateBegin": "2026-13-08T00:00:00"},
])
def test_flight_recommendations_modified_dates_and_invalid_dates_are_ignored(changes):
    data = {
        "header": "Строительство новых очередей", "link": "actual-article",
        "dateBegin": "2026-11-08T00:00:00", **changes,
    }
    page = load(ld(article()) + flight(data))
    assert page.published_at is None


def test_invalid_json_and_executable_scripts_are_not_interpreted():
    page = load(
        ld(article())
        + '<script type="application/ld+json">{not JSON}</script>'
        + '<script>self.__next_f.push(alert("2026-11-08"))</script>'
    )
    assert page.text == BODY.strip()
    assert page.published_at is None
