"""Renderer policy tests with a stub browser: no actual PDF or browser generation."""

import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from radar.reports import render as renderer

ROOT = Path(__file__).resolve().parents[1]


class Browser:
    def __init__(self, states, *, html_pages=6, pdf_pages=6):
        self.states = list(states)
        self.html_pages, self.pdf_pages = html_pages, pdf_pages
        self.styles, self.marked, self.routes = [], [], []
        self.checks = self.pdf_calls = 0
        self.closed = False
        self.chromium = self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def launch(self):
        return self

    def new_page(self, **_kwargs):
        return self

    def route(self, pattern, _handler):
        self.routes.append(pattern)

    def goto(self, url):
        self.html = Path(unquote(urlsplit(url).path)).read_text()

    def wait_for_load_state(self, _state):
        pass

    def evaluate(self, script, numbers=None):
        if script == renderer.OVERFLOW_JS:
            self.checks += 1
            return self.states.pop(0)
        if script == renderer.V8_FIT_JS:
            self.marked.append(numbers)

    def add_style_tag(self, *, content):
        self.styles.append(content)

    def locator(self, selector):
        assert selector == ".page"
        return self

    def count(self):
        return self.html_pages

    def pdf(self, **_kwargs):
        self.pdf_calls += 1
        return b"%PDF-fixture\n" + b"/Type /Page\n" * self.pdf_pages + b"%%EOF"

    def close(self):
        self.closed = True


@pytest.fixture
def report():
    return json.loads((ROOT / "src/radar/reports/gold/v8/nametkin-tower.json").read_text())


def issue(page=1, kind="height"):
    return {"page": page, "name": "situation", "block": "body", "kind": kind, "over_px": 12}


def install(monkeypatch, states, **options):
    browser = Browser(states, **options)
    monkeypatch.setattr(renderer, "sync_playwright", lambda: browser)
    return browser


def test_one_spacing_variant_only_marks_overflowed_pages_and_keeps_all_copy(monkeypatch, report):
    browser = install(monkeypatch, [[issue(), issue(5), issue(5, "bottom")], []])
    binary, overflow = renderer.render(report)
    assert binary.startswith(b"%PDF") and overflow == []
    assert browser.marked == [[1, 5]] and browser.styles == [renderer.V8_FIT_CSS]
    assert browser.checks == 2 and browser.pdf_calls == 1 and browser.closed
    assert report["situation"]["lead"] in browser.html
    assert browser.routes == ["http://**", "https://**"]


def test_persistent_overflow_is_not_exported_or_persisted(monkeypatch, report, tmp_path):
    overflow = [issue()]
    browser = install(monkeypatch, [overflow, overflow])
    target = tmp_path / "must-not-exist.pdf"
    assert renderer.render(report, output=target) == (b"", overflow)
    assert browser.checks == 2 and len(browser.styles) == 1
    assert browser.pdf_calls == 0 and browser.closed and not target.exists()


def test_fitting_is_not_applied_to_an_already_fitting_document(monkeypatch, report):
    browser = install(monkeypatch, [[]])
    _, overflow = renderer.render(report)
    assert overflow == [] and not browser.styles and not browser.marked
    assert browser.checks == browser.pdf_calls == 1


def test_vertical_spacing_does_not_pretend_to_fix_horizontal_overflow(monkeypatch, report):
    overflow = [issue(kind="width")]
    browser = install(monkeypatch, [overflow])
    assert renderer.render(report) == (b"", overflow)
    assert not browser.styles and browser.checks == 1 and browser.pdf_calls == 0


def test_spacing_variant_cannot_reduce_typography_hide_copy_or_move_fixed_tree():
    for selector, declarations in re.findall(r"([^{}]+)\{([^{}]+)\}", renderer.V8_FIT_CSS):
        selector = selector.strip()
        assert selector.startswith(".page.is-fit-compact[data-page] ")
        if selector.endswith(" .risk"):
            assert selector == ".page.is-fit-compact[data-page] .lv--1 .risk"
        for declaration in declarations.split(";"):
            if not declaration.strip():
                continue
            name, value = declaration.split(":")
            assert name in {"gap", "padding-top", "padding-bottom", "margin-top", "margin-bottom"}
            assert re.fullmatch(r"(?:\d+|\d*\.\d+)mm", value)


def test_v8_never_exports_more_than_six_pages(monkeypatch, report):
    browser = install(monkeypatch, [[]], html_pages=7)
    with pytest.raises(ValueError, match="шесть страниц"):
        renderer.render(report)
    assert browser.pdf_calls == 0 and browser.closed


def test_pdf_page_count_must_match_the_verified_html(monkeypatch, report, tmp_path):
    browser = install(monkeypatch, [[]], pdf_pages=7)
    target = tmp_path / "must-not-exist.pdf"
    with pytest.raises(ValueError, match="Число страниц PDF"):
        renderer.render(report, output=target)
    assert browser.pdf_calls == 1 and browser.closed and not target.exists()


def test_historical_v5_keeps_its_original_render_path(monkeypatch):
    old = json.loads((ROOT / "src/radar/reports/gold/nametkin-tower.json").read_text())
    overflow = [issue()]
    browser = install(monkeypatch, [overflow])
    binary, result = renderer.render(old)
    assert binary.startswith(b"%PDF") and result == overflow
    assert not browser.styles and not browser.marked and browser.pdf_calls == 1
