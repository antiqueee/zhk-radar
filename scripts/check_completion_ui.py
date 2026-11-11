"""Isolated browser acceptance: advanced tools never touch the user's workspace data."""

import json
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

output = Path("data/qa")
output.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix="radar-ui-") as folder:
    root = Path(folder)
    shutil.copytree("config", root / "config")
    code = (
        "from pathlib import Path; import uvicorn; from radar.web.app import create_app; "
        'import sys; uvicorn.run(create_app(Path(sys.argv[1])), host="127.0.0.1", port=8766)'
    )
    log = (root / "server.log").open("w+")
    server = subprocess.Popen([sys.executable, "-c", code, folder], stdout=log, stderr=log)
    try:
        for _ in range(60):
            try:
                if httpx.get("http://127.0.0.1:8766/healthz").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            log.seek(0)
            raise RuntimeError("Isolated application did not start: " + log.read())
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto("http://127.0.0.1:8766")
            page.locator("#dataset").select_option("demo")
            page.locator('.record-name[data-open="demo-1"]').click()
            page.get_by_text("Реестр корпуса и история сроков", exact=True).click()
            page.locator('#complete-binding [name="house_id"]').fill("12345")
            page.locator('#complete-binding [name="confirmed"]').check()
            page.get_by_role("button", name="Сохранить привязку").click()
            expect(page.locator("#toast")).to_have_text("Сохранено")
            raw = {
                "house_id": "12345",
                "name": "Учебный корпус для проверки интерфейса",
                "commissioning": "2027-01-01",
                "readiness": 70,
                "source_url": "https://example.org/synthetic",
                "observed_at": datetime.now(UTC).isoformat(),
            }
            page.locator("#registry-file").set_input_files(
                {
                    "name": "snapshot.json",
                    "mimeType": "application/json",
                    "buffer": json.dumps(raw).encode(),
                }
            )
            expect(page.locator("#toast")).to_have_text("Снимок загружен")
            with page.expect_download() as download:
                page.get_by_role("button", name="Получить PDF-разбор", exact=False).click()
            download.value.save_as(str(output / "registry-demo.pdf"))
            page.locator("#brief-check > summary").click()
            page.get_by_role("button", name="Проверить моделью").first.click()
            expect(page.locator("#brief-check")).to_contain_text("Нужна проверка аналитика")
            page.screenshot(path=str(output / "completion-card.png"))
            page.get_by_text("Исключить из предложений", exact=True).click()
            page.get_by_role("button", name="Добавить в стоп-лист").click()
            expect(page.locator("#detail [data-pdf]").first).to_be_disabled()
            page.get_by_role("button", name="Закрыть карточку").click()
            expect(page.locator('.record-name[data-open="demo-1"]')).to_have_count(0)
            page.locator("#show-blocked").check()
            page.locator('.record-name[data-open="demo-1"]').click()
            page.get_by_text("Исключить из предложений", exact=True).click()
            page.get_by_role("button", name="Снять ограничение").click()
            expect(page.locator("#detail [data-pdf]").first).to_be_enabled()
            page.get_by_role("button", name="Закрыть карточку").click()
            page.get_by_role("button", name="Подключения", exact=False).click()
            page.locator('[name="review_model"]').fill("test/reviewer")
            page.get_by_role("button", name="Сохранить подключения").click()
            expect(page.locator("#settings-status")).to_contain_text("Подключения сохранены")
            page.get_by_text("Google Таблица — синхронизация", exact=True).click()
            page.get_by_role("button", name="Синхронизировать таблицу").click()
            expect(page.locator("#toast")).to_contain_text("ID Google Таблицы")
            page.get_by_text("Резервные копии", exact=True).click()
            page.get_by_role("button", name="Создать копию сейчас").click()
            expect(page.locator("#backup-result")).to_contain_text("Проверенная копия")
            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path=str(output / "completion-mobile.png"))
            assert page.locator("#connections").evaluate("e=>e.scrollWidth<=e.clientWidth")
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            assert not errors, errors
            browser.close()
            print(
                "Completion UI passed: registry/PDF, review, stop-list, "
                "settings, backups and mobile."
            )
    finally:
        server.terminate()
        server.wait(timeout=15)
