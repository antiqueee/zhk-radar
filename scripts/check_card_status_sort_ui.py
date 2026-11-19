"""Isolated browser acceptance for registry sorting and pagination."""

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

ORIGIN = "http://127.0.0.1:8772"


def record(ident, status, report=None, **fields):
    row = {"id": ident, "name": f"ЖК {ident}", "status": status, "dataset": "web", **fields}
    if report:
        code, label = report
        row["report_status"] = {"code": code, "label": label}
    return row


records = [
    record("no-report-a", "new"),
    record("ready-a", "review", ("ready", "Отчёт готов")),
    record("partial", "later", ("partial", "Поиск завершён частично")),
    record("manual", "inwork", ("manual", "Нужна редакторская проверка"), name="ЖК Сад А"),
    record("watch", "closed", ("watch", "Объект оставлен под наблюдением")),
    record("hold", "new", ("hold", "Нужна проверка объекта")),
    record("ready-b", "meeting", ("ready", "Отчёт готов"), name="ЖК Сад Б"),
    record("retry", "proposal", ("retry", "Не удалось разобрать ответ модели")),
    record("old", "new", ("outdated", "Прежний формат PDF")),
    record("no-report-b", "other"),
    record("no-report-c", "new"),
    record("no-report-d", "new"),
    record("blocked", "new", blocked={"kind": "object"}),
    record("rejected", "rejected"),
]


def attributes(locator, attribute):
    return locator.evaluate_all(
        "(elements, attr) => elements.map(el => el.getAttribute(attr))", attribute
    )


with tempfile.TemporaryDirectory(prefix="radar-status-sort-ui-") as folder:
    root = Path(folder)
    shutil.copytree("config", root / "config")
    code = (
        "from pathlib import Path; import sys, uvicorn; from radar.web.app import create_app; "
        'uvicorn.run(create_app(Path(sys.argv[1])),host="127.0.0.1",port=8772)'
    )
    with (root / "server.log").open("w+") as log:
        server = subprocess.Popen([sys.executable, "-c", code, folder], stdout=log, stderr=log)
        try:
            for _ in range(60):
                try:
                    if httpx.get(ORIGIN + "/healthz").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                log.seek(0)
                raise RuntimeError(log.read())

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.route(
                    ORIGIN + "/api/records?dataset=web", lambda route: route.fulfill(json=records)
                )
                page.goto(ORIGIN)

                cards = page.locator("#records .record")
                groups = page.locator("#records .record-group")
                sort = page.locator("#record-sort")
                expect(cards).to_have_count(10)
                expect(sort).to_have_value("report")
                assert attributes(groups, "data-status") == [
                    "ready",
                    "manual",
                    "hold",
                    "retry",
                    "partial",
                    "watch",
                    "outdated",
                    "none",
                ]
                assert attributes(cards, "data-card") == [
                    "ready-a",
                    "ready-b",
                    "manual",
                    "hold",
                    "retry",
                    "partial",
                    "watch",
                    "old",
                    "no-report-a",
                    "no-report-b",
                ]

                next_page = page.locator('[data-action="next"]')
                next_page.click()
                assert attributes(cards, "data-card") == ["no-report-c", "no-report-d"]
                assert attributes(groups, "data-status") == ["none"]

                search = page.locator("#filter")
                search.fill("сад")
                assert attributes(cards, "data-card") == ["ready-b", "manual"]
                assert attributes(groups, "data-status") == ["ready", "manual"]
                search.clear()

                hidden = page.locator("#show-blocked")
                hidden.check()
                next_page.click()
                assert attributes(groups, "data-status") == ["none", "hidden"]
                assert attributes(groups.last.locator(".record"), "data-card") == [
                    "blocked",
                    "rejected",
                ]
                hidden.uncheck()

                sort.select_option("card")
                assert attributes(groups, "data-status") == [
                    "new",
                    "review",
                    "inwork",
                    "proposal",
                    "meeting",
                    "closed",
                ]
                assert attributes(groups.first.locator(".record"), "data-card") == [
                    "no-report-a",
                    "hold",
                    "old",
                    "no-report-c",
                    "no-report-d",
                ]

                sort.select_option("priority")
                assert attributes(cards, "data-card") == [row["id"] for row in records[:10]]
                expect(groups).to_have_count(0)
                page.reload()
                expect(sort).to_have_value("priority")
                expect(cards).to_have_count(10)
                assert attributes(cards, "data-card") == [row["id"] for row in records[:10]]

                sort.select_option("report")
                page.set_viewport_size({"width": 390, "height": 844})
                expect(sort).to_be_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                assert not errors, errors
                browser.close()
                print("Card report/status/priority sorting, pagination, filters and mobile passed")
        finally:
            server.terminate()
            server.wait(timeout=10)
