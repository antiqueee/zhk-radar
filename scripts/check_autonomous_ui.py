"""Isolated browser acceptance for authored proposals and optional automation; no live APIs."""

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

from radar.web import cases, evaluation

output = Path("data/qa")
output.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix="radar-autonomy-ui-") as folder:
    root = Path(folder)
    shutil.copytree("config", root / "config")
    code = (
        "from pathlib import Path; import sys, uvicorn; from radar.web.app import create_app; "
        'uvicorn.run(create_app(Path(sys.argv[1])),host="127.0.0.1",port=8766)'
    )
    with (root / "server.log").open("w+") as log:
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
                raise RuntimeError(log.read())
            with sync_playwright() as p:
                browser = p.chromium.launch(channel="chrome", headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto("http://127.0.0.1:8766")
                page.locator("#dataset").select_option("demo")
                page.locator('.record-name[data-open="demo-1"]').click()
                expect(page.locator("#conversation-form")).to_be_visible()
                page.locator("#conversation-form textarea").fill("Почему перенесли сроки?")
                page.get_by_role("button", name="Задать вопрос", exact=True).click()
                expect(page.locator("#conversation-history")).to_contain_text(
                    "подключите OpenRouter", timeout=15000
                )
                page.locator("#analyst-tools > summary").click()
                page.locator("#advanced-card > summary").click()
                page.get_by_role(
                    "button", name="Подготовить предложение моделью", exact=True
                ).click()
                expect(page.locator("#object-task")).to_contain_text(
                    "демо не отправляется", timeout=15000
                )
                with page.expect_download() as download:
                    page.get_by_role("button", name="Получить PDF-разбор", exact=False).click()
                download.value.save_as(str(output / "autonomy-internal-demo.pdf"))
                expect(page.locator("#pdf-result")).to_contain_text("готова")
                page.locator("#analyst-tools").evaluate("el => el.open = true")
                page.locator("#brief-check > summary").click()
                with page.expect_popup() as popup:
                    page.get_by_text("Предпросмотр для девелопера ↗", exact=True).first.click()
                popup.value.close()
                page.locator("#extra-sources-card > summary").click()
                page.locator('#extra-source-binding [name="hh_employer"]').fill("11")
                page.locator('#extra-source-binding [name="confirmed"]').check()
                page.get_by_role("button", name="Сохранить источники", exact=True).click()
                expect(page.locator("#toast")).to_have_text("Источники сохранены")
                page.locator("#analyst-tools").evaluate("el => el.open = true")
                page.locator("#extra-sources-card").evaluate("el => el.open = true")
                page.get_by_role(
                    "button", name="Собрать дополнительные сигналы", exact=True
                ).click()
                expect(page.locator("#object-task")).to_contain_text(
                    "отключены для демо", timeout=15000
                )
                page.screenshot(path=str(output / "autonomy-card.png"))
                page.get_by_role("button", name="Закрыть карточку").click()
                page.get_by_role("button", name="Подключения", exact=False).click()
                page.locator("#model-settings > summary").click()
                page.locator('[name="author_model"]').select_option(index=1)
                page.get_by_role("button", name="Сохранить подключения", exact=True).click()
                expect(page.locator("#settings-status")).to_contain_text("Подключения сохранены")
                page.get_by_text("Раз в неделю и лимиты запросов", exact=True).click()
                expect(page.locator('#ops-weekly [name="screen_chats"]')).to_be_checked()
                page.locator('#ops-weekly [name="screen_model_calls"]').fill("0")
                page.get_by_role("button", name="Сохранить расписание и лимиты", exact=True).click()
                expect(page.locator("#weekly-status")).to_contain_text("расписание выключено")
                assert (
                    page.request.get("http://127.0.0.1:8766/api/operations").json()["settings"][
                        "screen_model_calls"
                    ]
                    == 0
                )
                page.get_by_text("Раз в неделю и лимиты запросов", exact=True).click()
                page.get_by_text("Денежный резерв бюджета", exact=True).click()
                page.locator('#advanced-budget [name="monthly_usd"]').fill("25")
                page.get_by_role("button", name="Сохранить бюджет", exact=True).click()
                expect(page.locator("#toast")).to_have_text("Настройки сохранены")
                page.get_by_text("Ночное чтение и уведомления", exact=True).click()
                page.locator('#advanced-automation [name="hour"]').fill("4")
                page.get_by_role("button", name="Сохранить автоматизацию", exact=True).click()
                expect(page.locator("#toast")).to_have_text("Настройки сохранены")
                page.get_by_text("Кейсы агентства", exact=True).click()
                case = cases.Case(
                    title="Учебный кейс для проверки формы",
                    situation="сроки",
                    topics=["разные_ответы"],
                    description="Учебное описание ситуации и работы агентства.",
                    result="Учебный результат: согласованные ответы команды.",
                    evidence="Синтетическая фикстура, не опыт агентства.",
                )
                page.locator("#case-file").set_input_files(
                    {
                        "name": "case.json",
                        "mimeType": "application/json",
                        "buffer": case.model_dump_json().encode(),
                    }
                )
                expect(page.locator("#toast")).to_have_text("Кейс сохранён")
                page.locator("#evaluation-settings > summary").click()
                page.locator("#evaluation-file").set_input_files(
                    {
                        "name": "evaluation.json",
                        "mimeType": "application/json",
                        "buffer": json.dumps(evaluation.example()).encode(),
                    }
                )
                expect(page.locator("#toast")).to_have_text("Оценка сохранена")
                page.locator("#evaluation-settings > summary").click()
                expect(page.locator("#evaluation-settings")).to_contain_text(
                    "Кандидат: не определён"
                )
                page.get_by_text("Внешние копии и дубль дайджеста на почту", exact=True).click()
                page.get_by_role("button", name="Выгрузить копию сейчас", exact=True).click()
                expect(page.locator("#toast")).to_contain_text("Заполните доступ к S3")
                page.set_viewport_size({"width": 390, "height": 844})
                page.screenshot(path=str(output / "autonomy-mobile.png"))
                assert page.locator("#connections").evaluate("e=>e.scrollWidth<=e.clientWidth")
                assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
                assert not errors, errors
                browser.close()
                print(
                    "Autonomous UI passed: task failures, PDFs, sources, cases, "
                    "budget, automation, quality and mobile."
                )
        finally:
            server.terminate()
            server.wait(timeout=15)
