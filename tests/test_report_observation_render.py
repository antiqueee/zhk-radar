from radar.reports.render import build_html, is_v8, is_v8_observation, page_count


def test_v8_observation_renders_sources_and_checks_without_an_offer():
    data = {
        "meta": {
            "schema_version": "8",
            "mode": "observation",
            "layout": "compact",
            "date": "19.11.2026",
        },
        "object": {"name": "Level Селигерская", "developer": "Level Group", "city": "Москва"},
        "qualification": {
            "label": "Объект оставлен под наблюдением",
            "detail": "Недостаточно датированных признаков текущего конфликта.",
            "dated_evidence": 1,
            "active_contours": ["media"],
            "manual_checks": ["Сверить актуальную редакцию проектной декларации."],
        },
        "sources": [
            {
                "n": 1,
                "publisher": "Реестр новостроек",
                "title": "Декларация проекта",
                "date": "12.11.2026",
                "url": "https://example.org/registry?phase=2&doc=1",
            },
            {
                "n": 2,
                "publisher": "Историческая запись",
                "title": "Архивная публикация",
                "date": "",
                "url": "javascript:alert(1)",
            },
        ],
        "signals": [{"text": "Событие из реестра.", "source_n": 1, "date": "12.11.2026"}],
        "official": [
            {
                "text": "Формат & источник.",
                "title": "Карточка застройщика",
                "url": "https://example.org/official",
            }
        ],
    }

    html = build_html(data)

    assert is_v8(data) and is_v8_observation(data)
    assert html.count('class="page') == page_count(data) == 3
    assert "template/v8/report.css" in html
    assert "Объект оставлен под наблюдением" in html
    assert "Сверить актуальную редакцию проектной декларации" in html
    assert 'href="https://example.org/registry?phase=2&amp;doc=1"' in html
    assert 'href="https://example.org/official"' in html and "Формат &amp; источник" in html
    assert "javascript:" not in html
    assert "Стадия конфликта" not in html and "Коммерческое предложение" not in html
