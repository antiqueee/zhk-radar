"""Browser checks for QR presentation with stubbed Telegram; no real tokens or login."""

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

from radar.web.telegram_qr import qr_image

with tempfile.TemporaryDirectory(prefix="radar-qr-ui-") as folder:
    root = Path(folder)
    shutil.copytree("config", root / "config")
    code = (
        "from pathlib import Path; import sys, uvicorn; from radar.web.app import create_app; "
        'uvicorn.run(create_app(Path(sys.argv[1])),host="127.0.0.1",port=8767)'
    )
    with (root / "server.log").open("w+") as log:
        server = subprocess.Popen([sys.executable, "-c", code, folder], stdout=log, stderr=log)
        try:
            for _ in range(100):
                try:
                    if httpx.get("http://127.0.0.1:8767/healthz").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError("Test server did not start")
            with sync_playwright() as p:
                browser = p.chromium.launch(channel="chrome", headless=True)
                page = browser.new_page(viewport={"width": 1100, "height": 900})
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                state = {"status": "idle", "message": "Нажмите «Войти по QR-коду»."}

                def route_qr(route):
                    if route.request.method == "POST" and route.request.url.endswith("/password"):
                        if json.loads(route.request.post_data)["password"] != "test-password":
                            route.fulfill(status=400, json={"detail": "Неверный пароль"})
                            return
                        state.clear()
                        state.update(status="authorized", message="Вход выполнен.")
                    elif route.request.method == "POST":
                        state.update(
                            status="waiting",
                            image=qr_image("tg://login?token=TEST-NOT-VALID"),
                            message="Отсканируйте QR-код в Telegram.",
                        )
                    elif route.request.method == "DELETE":
                        state.clear()
                        state.update(status="idle", message="Вход отменён.")
                    route.fulfill(json=state)

                page.route("**/api/telegram/qr**", route_qr)
                page.goto("http://127.0.0.1:8767")
                page.get_by_role("button", name="Подключения", exact=False).click()
                page.get_by_text("Вход в Telegram", exact=True).click()
                expect(page.locator("#ops-tg-qr-password")).to_be_hidden()
                page.get_by_role("button", name="Войти по QR-коду", exact=True).click()
                expect(page.locator("#tg-qr-result img")).to_be_visible()
                assert page.locator("#tg-qr-result img").evaluate(
                    "e=>e.complete && e.naturalWidth>0"
                )
                out = Path("data/qa")
                out.mkdir(parents=True, exist_ok=True)
                page.locator("#tg-qr-result").screenshot(path=str(out / "telegram-qr-demo.png"))
                page.set_viewport_size({"width": 390, "height": 844})
                page.screenshot(path=str(out / "telegram-qr-mobile-demo.png"))
                assert page.locator("#connections").evaluate("e=>e.scrollWidth<=e.clientWidth")
                page.get_by_role("button", name="Отменить вход по QR", exact=True).click()
                expect(page.locator("#tg-qr-result img")).to_have_count(0)
                page.get_by_role("button", name="Войти по QR-коду", exact=True).click()
                state.clear()
                state.update(status="password_needed", message="Введите пароль Telegram.")
                expect(page.locator("#ops-tg-qr-password")).to_be_visible(timeout=5000)
                expect(page.locator("#tg-qr-result img")).to_have_count(0)
                page.locator("#ops-tg-qr-password input").fill("wrong")
                page.get_by_role("button", name="Подтвердить пароль", exact=True).click()
                expect(page.locator("#tg-qr-result")).to_contain_text("Неверный пароль")
                expect(page.locator("#ops-tg-qr-password input")).to_have_value("")
                page.locator("#ops-tg-qr-password input").fill("test-password")
                page.get_by_role("button", name="Подтвердить пароль", exact=True).click()
                expect(page.locator("#tg-qr-result")).to_contain_text("Вход выполнен")
                expect(page.locator("#ops-tg-qr-password")).to_be_hidden()
                assert not errors, errors
                browser.close()
        finally:
            server.terminate()
            server.wait(timeout=15)
print("QR UI passed: image, mobile, cancel, password retry and successful login (mock).")
