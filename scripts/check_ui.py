"""Acceptance check against local UI. Mutates synthetic demo records only."""

from pathlib import Path

from playwright.sync_api import expect, sync_playwright

output = Path("data/qa")
output.mkdir(parents=True, exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1050}, device_scale_factor=1)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto("http://127.0.0.1:8765")
    page.wait_for_selector(".record")
    expect(page.locator(".brand")).to_contain_text("Мегаполис")
    assert page.locator(".sidebar").count() == 0
    assert page.locator(".record").count() == 10
    page.screenshot(path=str(output / "overview.png"))
    page.get_by_role("button", name="Подключения", exact=False).click()
    expect(page.locator("#connections")).to_be_visible()
    expect(page.locator('input[name="YANDEX_SEARCH_API_KEY"]')).to_have_attribute(
        "type", "password"
    )
    expect(page.get_by_role("button", name="Сохранить подключения")).to_be_visible()
    page.screenshot(path=str(output / "connections.png"))
    page.get_by_role("button", name="Закрыть подключения").click()
    page.locator("#dataset").select_option("demo")
    page.wait_for_selector('[data-open="demo-1"]')
    page.locator('.record-name[data-open="demo-1"]').click()
    page.get_by_text("Уточнить ситуацию и предложение", exact=True).click()
    page.locator('#decision-form textarea[name="note"]').fill(
        "Уточнить график информирования жителей"
    )
    page.get_by_role("button", name="Сохранить уточнения").click()
    expect(page.locator("#toast")).to_have_text("Уточнения сохранены")
    page.get_by_role("button", name="Попробовать на примере").click()
    expect(page.locator(".chat-stats")).to_contain_text("42")
    with page.expect_download() as download_info:
        page.get_by_role("button", name="Получить PDF-разбор", exact=False).click()
    download_info.value.save_as(str(output / "demo.pdf"))
    page.wait_for_selector("#pdf-result a")
    page.screenshot(path=str(output / "detail.png"))
    page.get_by_role("button", name="Закрыть карточку").click()
    page.locator("#filter").fill("несуществующий объект")
    expect(page.locator(".record")).to_have_count(0)
    page.locator("#filter").fill("Северный")
    expect(page.locator(".record")).to_have_count(1)
    page.locator("#dataset").select_option("imported")
    page.wait_for_selector(".record")
    page.set_viewport_size({"width": 390, "height": 844})
    page.screenshot(path=str(output / "mobile.png"))
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.get_by_role("button", name="Подключения", exact=False).click()
    expect(page.locator("#connections")).to_be_visible()
    assert page.locator("#connections").evaluate("e => e.scrollWidth <= e.clientWidth")
    assert not errors, errors
    print("Simple flow passed: branding, settings, PDF download, chat import, filters and mobile.")
    browser.close()
