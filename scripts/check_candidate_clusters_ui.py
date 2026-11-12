"""Isolated acceptance of full-card navigation and reversible exclusions. No paid APIs."""

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

output = Path("data/qa")
output.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix="radar-clusters-ui-") as folder:
    root = Path(folder)
    shutil.copytree("config", root / "config")
    code = (
        "from pathlib import Path; import sys, uvicorn; from radar.web.app import create_app; "
        'uvicorn.run(create_app(Path(sys.argv[1])),host="127.0.0.1",port=8767)'
    )
    with (root / "server.log").open("w+") as log:
        server = subprocess.Popen([sys.executable, "-c", code, folder], stdout=log, stderr=log)
        try:
            for _ in range(60):
                try:
                    if httpx.get("http://127.0.0.1:8767/healthz").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                log.seek(0)
                raise RuntimeError(log.read())
            with sync_playwright() as p:
                browser = p.chromium.launch(channel="chrome", headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto("http://127.0.0.1:8767")
                page.locator("#dataset").select_option("demo")
                card = page.locator('[data-card="demo-1"]')
                card.locator(".signal").click()
                expect(page.locator("#detail")).to_be_visible()
                expect(page.locator("#detail-title")).to_contain_text("Северный берег")
                page.locator('[data-action="close-detail"]').click()
                card.locator("[data-disposition]").click()
                expect(card).to_have_count(0)
                expect(page.locator("#detail")).not_to_be_visible()
                expect(page.locator("#hidden-count")).to_have_text("1")
                page.reload()
                expect(page.locator("#hidden-count")).to_have_text("1")
                expect(card).to_have_count(0)
                page.locator("#show-blocked").check()
                expect(card).to_be_visible()
                card.get_by_role("button", name="Вернуть в работу").click()
                expect(page.locator("#hidden-count")).to_have_text("0")
                page.locator("#show-blocked").uncheck()
                expect(card).to_be_visible()
                card.focus()
                page.keyboard.press("Enter")
                expect(page.locator("#detail")).to_be_visible()
                page.locator('[data-action="close-detail"]').click()
                with page.expect_download():
                    card.locator("[data-pdf]").click()
                expect(page.locator("#detail")).not_to_be_visible()
                page.screenshot(path=str(output / "clusters-desktop.png"), full_page=True)
                page.set_viewport_size({"width": 390, "height": 844})
                card.locator(".signal").click()
                expect(page.locator("#detail")).to_be_visible()
                expect(page.locator("#detail [data-disposition]").first).to_be_visible()
                page.screenshot(path=str(output / "clusters-mobile.png"), full_page=True)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                assert not errors, errors
                browser.close()
                print(
                    "Full-card click, keyboard, hide/reload/restore, "
                    "PDF isolation and mobile passed"
                )
        finally:
            server.terminate()
            server.wait(timeout=10)
