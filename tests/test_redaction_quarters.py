import pytest

from radar.web.chats import redact
from radar.web.sources import fetch_page


@pytest.mark.parametrize(
    "text",
    [
        "Литер21III кв.2026IV кв.2026+1 кв.",
        "Литер 21III кв.2026IV кв.2026+1 кв.",
        "IV кв.2026Ближайший плановый ввод",
        "II кв.2026",
        "III кв. 2026 IV кв. 2027",
        "1 кв.2026",
    ],
)
def test_explicit_calendar_quarters_keep_the_original_text(text):
    assert redact(text, []) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("кв.2026", "[квартира]"),
        ("кв. 2026", "[квартира]"),
        ("квартира2026", "[квартира]"),
        ("квартира 2026", "[квартира]"),
        ("кв.21а", "[квартира]"),
        ("III кв.2026а", "III [квартира]"),
        ("VIII кв.2026", "VIII [квартира]"),
        ("12 кв.2026", "12 [квартира]"),
    ],
)
def test_apartments_and_nonquarter_contexts_remain_private(text, expected):
    assert redact(text, []) == expected


def test_minified_table_cells_do_not_glue_building_quarters_and_headings():
    html = (
        "<html><title>Строительство ЖК</title><article>"
        "<p>Плановые сроки ввода корпусов и журнал изменений.</p>"
        "<table><tr><th>Корпус</th><th>Было</th><th>Стало</th><th>Сдвиг</th></tr>"
        "<tr><td>Литер 21</td><td>III кв.2026</td><td>IV кв.2026</td><td>+1 кв.</td></tr>"
        "</table></article></html>"
    )
    page = fetch_page(
        "https://example.org/building",
        lambda _url: (200, {"content-type": "text/html"}, html.encode()),
    )
    assert page.status == "loaded"
    assert "Литер 21\nIII кв.2026\nIV кв.2026\n+1 кв." in page.text
    assert redact(page.text, []) == page.text
