"""Offline acceptance of report versions/progress/download errors and chat retries."""

import io
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright
from pypdf import PdfWriter

pdf_file = io.BytesIO()
writer = PdfWriter()
writer.add_blank_page(width=595, height=842)
writer.write(pdf_file)
created = "2026-11-16T16:00:00+00:00"
old = {
    "id": 50, "version": 1, "format_version": "5", "current_format": False,
    "downloadable": True, "created_at": created, "payload": {},
}
current = {
    "id": 51, "version": 2, "format_version": "8", "current_format": True,
    "downloadable": True, "created_at": created, "payload": {},
}
draft = {
    "id": 52, "version": 3, "format_version": "8", "current_format": True,
    "downloadable": False, "created_at": created, "label": "Нужна редакторская проверка",
    "detail": "Проверьте выводы", "payload": {"report_pipeline": {"status": "manual"}},
}
saved_draft = {**draft, "previous_approved_v8": {"id": 51, "version": 2, "created_at": created}}
saved_model_error = {
    "id": 53, "version": 4, "format_version": "8", "current_format": True,
    "downloadable": False, "retryable": True, "created_at": created,
    "label": "Не удалось разобрать ответ модели",
    "detail": "Повторите сборку отчёта. Сервис использует сохранённые материалы.",
    "payload": {"report_pipeline": {"status": "manual", "issue_kind": "model_output"}},
}
retried = {**current, "id": 54, "version": 5}

with tempfile.TemporaryDirectory(prefix="radar-report-ui-") as folder:
    root = Path(folder)
    shutil.copytree("config", root / "config")
    code = (
        "from pathlib import Path; import sys, uvicorn; from radar.web.app import create_app; "
        'uvicorn.run(create_app(Path(sys.argv[1])),host="127.0.0.1",port=8768)'
    )
    with (root / "server.log").open("w+") as log:
        server = subprocess.Popen([sys.executable, "-c", code, folder], stdout=log, stderr=log)
        try:
            for _ in range(60):
                try:
                    if httpx.get("http://127.0.0.1:8768/healthz").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                log.seek(0)
                raise RuntimeError(log.read())
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.clock.install()
                errors, generated, questions, blocked_pdf_requests = [], [], [], []
                page.on("pageerror", lambda error: errors.append(str(error)))
                mode = {"latest": "old", "poll": 0, "pdf_error": False, "task_report": "current"}
                cancel_requests = []
                report_task_requests = []
                turns = []

                def mock(route):
                    request = route.request
                    path = request.url.split("127.0.0.1:8768", 1)[-1]
                    if path == "/api/records?dataset=demo":
                        response = route.fetch()
                        records = response.json()
                        for record in records:
                            if record["id"] == "demo-1":
                                record["dataset"] = "web"
                                record["possible_duplicates"] = ["imported-canonical"]
                        route.fulfill(response=response, json=records)
                    elif path == "/api/records/demo-1" and request.method == "GET":
                        response = route.fetch()
                        record = response.json()
                        record["possible_duplicates"] = ["imported-canonical"]
                        record["chat_stats"].update(
                            messages_30d=92, labelled=0,
                            chats=[{"chat_id": "read-chat", "title": "Открытый чат"}],
                            members=99999,
                        )
                        record["report_research"] = {
                            "version": "object-research-v5", "created_at": created,
                            "completed": False, "documents": [
                                {"title": "Прочитанная статья", "url": "https://example.org/read",
                                 "published_at": "2025-02-03", "read_status": "loaded",
                                 "source_kind": "page", "contour": "media"},
                                {"title": "Недоступная страница", "url": "https://example.org/blocked",
                                 "published_at": None, "read_status": "unavailable",
                                 "source_kind": "page", "contour": "media"},
                                {"title": "Только выдача", "url": "https://example.org/snippet",
                                 "published_at": None, "read_status": "loaded",
                                 "source_kind": "snippet", "contour": "media"},
                            ],
                        }
                        route.fulfill(response=response, json=record)
                    elif path in {
                        "/api/records/imported-canonical", "/api/records/historical-alias",
                    }:
                        response = route.fetch(url="http://127.0.0.1:8768/api/records/demo-2")
                        record = response.json()
                        record.update(id="imported-canonical", dataset="imported",
                                      name="Единая карточка из импорта")
                        route.fulfill(response=response, json=record)
                    elif path == "/api/records/demo-1/brief/latest":
                        recent = {
                            "old": old, "current": current, "draft": saved_draft,
                            "model_error": saved_model_error, "retried": retried,
                        }
                        brief = recent.get(mode["latest"])
                        if brief is None:
                            route.fulfill(body="null", content_type="application/json")
                        else:
                            route.fulfill(json=brief)
                    elif path == "/api/records/demo-1/tasks":
                        generated.append(request.post_data_json)
                        route.fulfill(json={"id": "ui-author", "status": "queued"})
                    elif path == "/api/tasks/ui-author/cancel":
                        cancel_requests.append(request.method)
                        mode["cancel"] = "stopping"
                        route.fulfill(json={
                            "id": "ui-author", "status": "running", "cancel_requested": True,
                            "created_at": created, "updated_at": created,
                        })
                    elif path == "/api/tasks/ui-author":
                        if cancel := mode.get("cancel"):
                            stopped = cancel == "cancelled"
                            route.fulfill(json={
                                "id": "ui-author", "status": "cancelled" if stopped else "running",
                                "cancel_requested": cancel == "stopping",
                                "created_at": created, "updated_at": created,
                                "error": (
                                    "Сохранённые этапы доступны для повтора." if stopped else None
                                ),
                                "progress": {"completed": 2, "label": "Читаем источники"},
                            })
                            return
                        mode["poll"] += 1
                        complete = mode["poll"] >= 2
                        if complete:
                            mode["latest"] = mode["task_report"]
                        route.fulfill(json={
                            "id": "ui-author", "status": "completed" if complete else "running",
                            "created_at": created, "updated_at": created,
                            "result": {
                                "old": old, "current": current, "draft": draft, "retried": retried,
                            }[mode["task_report"]]
                            if complete else None,
                            "progress": {"completed": 6 if complete else 2,
                                         "label": "PDF готов" if complete else "Читаем источники",
                                         "detail": "Сохранённые материалы"},
                        })
                    elif path in {"/api/briefs/50/pdf", "/api/briefs/51/pdf", "/api/briefs/54/pdf"}:
                        if mode["pdf_error"] == "html":
                            route.fulfill(content_type="text/html", body="<html>Login</html>")
                        elif mode["pdf_error"]:
                            route.fulfill(status=409, json={"detail": "Версия требует проверки"})
                        else:
                            route.fulfill(content_type="application/pdf", body=pdf_file.getvalue())
                    elif path == "/api/briefs/53/pdf":
                        blocked_pdf_requests.append(path)
                        route.fulfill(status=409, json={"detail": saved_model_error["label"]})
                    elif path.endswith("/conversation"):
                        if request.method == "POST":
                            questions.append(request.post_data_json)
                            if len(questions) == 1:
                                route.abort("connectionreset")
                                return
                            turns.append({
                                "id": "turn-ui", "question": questions[-1]["question"],
                                "status": "completed", "answer": {
                                    "sections": [{"title": "Ответ по объекту", "kind": "proposal",
                                                  "text": "Подготовить адресное объяснение сроков.",
                                                  "source_ids": []}],
                                    "sources": [], "source_count": 0, "available_sources": 0,
                                    "queries": [],
                                },
                            })
                            route.fulfill(status=202, json={"id": "turn-ui"})
                        else:
                            route.fulfill(json=turns if "/demo-1/" in path else [])
                    elif path.endswith("/report-task"):
                        report_task_requests.append(path)
                        route.fulfill(json=None)
                    else:
                        route.continue_()

                page.route("**/api/**", mock)
                page.goto("http://127.0.0.1:8768")
                page.locator("#dataset").select_option("demo")
                card = page.locator('[data-card="demo-1"]')
                expect(card).to_contain_text("Возможный дубль")
                downloads = []
                page.on("download", lambda download: downloads.append(download))
                card.locator("[data-pdf]").click()
                expect(page.locator("#pdf-result")).to_contain_text("прежнего образца")
                expect(page.locator("#duplicate-links")).to_contain_text("Открыть похожую карточку")
                overview = page.locator("#research-overview")
                expect(overview).to_contain_text("Прочитано сообщений за 30 дней: 92")
                expect(overview).not_to_contain_text("Чаты не прочитаны")
                expect(overview).not_to_contain_text("99999")
                materials = page.locator("#extended-research")
                assert materials.get_attribute("open") is None
                materials.locator("summary").click()
                expect(materials).to_contain_text("03.02.2025")
                expect(materials).to_contain_text("Текст загружен")
                expect(materials).to_contain_text("Полный текст недоступен")
                expect(materials).to_contain_text("Фрагмент выдачи")
                expect(materials.locator('a[href="https://example.org/read"]')).to_be_visible()
                assert not downloads and not generated
                page.locator("#pdf-result").get_by_role("button", name="Подготовить PDF v8").click()
                expect(page.locator("#report-progress progress")).to_be_visible()
                expect(page.locator("#report-progress")).to_contain_text("Читаем источники")
                page.clock.fast_forward(1600)
                expect(page.locator("#pdf-result")).to_contain_text(
                    "Версия 2 готова", timeout=15000
                )
                assert len(downloads) == 1 and len(generated) == 1
                # A server-side rejection is shown, never saved as an apparent PDF.
                mode["pdf_error"] = True
                page.locator("#detail .pdf-action [data-pdf]").first.click()
                expect(page.locator("#pdf-result")).to_contain_text("Версия требует проверки")
                assert len(downloads) == 1
                mode["pdf_error"] = "html"
                page.locator("#detail .pdf-action [data-pdf]").first.click()
                expect(page.locator("#pdf-result")).to_contain_text(
                    "Сервер вернул файл другого типа"
                )
                assert len(downloads) == 1
                # An uncertain network retry keeps the idempotency key.
                question = page.locator("#conversation-form textarea")
                question.fill("Что объяснить дольщикам по срокам?")
                ask = page.get_by_role("button", name="Задать вопрос", exact=True)
                ask.click()
                expect(page.locator("#conversation-status")).not_to_be_empty()
                expect(ask).to_be_enabled()
                ask.click()
                expect(page.locator("#conversation-history")).to_contain_text("Ответ по объекту")
                assert len(questions) == 2
                assert questions[0]["request_id"] == questions[1]["request_id"]
                page.locator('[data-action="close-detail"]').click()
                card.locator(".signal").click()
                expect(page.locator("#conversation-history")).to_contain_text("Ответ по объекту")
                page.locator('[data-action="close-detail"]').click()
                page.locator('[data-card="demo-2"] .signal').click()
                expect(page.locator("#conversation-history")).to_be_empty()
                expect(page.locator("#research-overview")).to_contain_text("Чаты не прочитаны")
                # Starting a real report from the list must expose its progress too.
                page.locator('[data-action="close-detail"]').click()
                mode.update(latest="none", poll=0, pdf_error=False)
                card.locator("[data-pdf]").click()
                expect(page.locator("#detail")).to_be_visible()
                expect(page.locator("#report-progress")).to_contain_text("Читаем источники")
                page.clock.fast_forward(1600)
                expect(page.locator("#pdf-result")).to_contain_text(
                    "Версия 2 готова", timeout=15000
                )
                assert len(downloads) == 2 and len(generated) == 2
                # Generation already in flight during an upgrade can finish in the old format.
                mode.update(latest="none", poll=0, task_report="old")
                page.locator("#detail .pdf-action [data-pdf]").first.click()
                expect(page.locator("#report-progress")).to_contain_text("Читаем источники")
                page.clock.fast_forward(1600)
                expect(page.locator("#pdf-result")).to_contain_text("прежнего образца")
                assert len(downloads) == 2 and len(generated) == 3
                mode.update(latest="none", poll=0, task_report="draft")
                page.locator("#detail .pdf-action [data-pdf]").first.click()
                expect(page.locator("#report-progress")).to_contain_text("Читаем источники")
                page.clock.fast_forward(1600)
                for target in ("#pdf-result", "#report-progress"):
                    result = page.locator(target)
                    expect(result).to_contain_text("Нужна редакторская проверка")
                    approved_link = result.get_by_role(
                        "link", name="Скачать ранее одобренный PDF v8"
                    )
                    expect(approved_link).to_have_attribute("href", "/api/briefs/51/pdf")
                assert len(downloads) == 2 and len(generated) == 4
                with page.expect_download():
                    page.locator("#pdf-result").get_by_role(
                        "link", name="Скачать ранее одобренный PDF v8"
                    ).click()
                # A saved model error opens the card and waits for an explicit retry.
                page.locator('[data-action="close-detail"]').click()
                mode.update(latest="model_error", poll=0, task_report="retried")
                card.locator("[data-pdf]").click()
                expect(page.locator("#detail")).to_be_visible()
                result = page.locator("#pdf-result")
                expect(result).to_contain_text("Не удалось разобрать ответ модели")
                expect(result).to_contain_text(
                    "Повторите сборку отчёта. Сервис использует сохранённые материалы."
                )
                retry = result.get_by_role("button", name="Повторить сборку PDF", exact=True)
                expect(retry).to_be_enabled()
                assert len(generated) == 4 and not blocked_pdf_requests
                with page.expect_download():
                    retry.click()
                    expect(page.locator("#report-progress")).to_contain_text("Читаем источники")
                    page.clock.fast_forward(1600)
                expect(result).to_contain_text("Версия 5 готова")
                assert len(generated) == 5 and generated[-1] == {"kind": "author"}
                assert not blocked_pdf_requests
                # Cancelling a rebuild targets its known task and leaves the ready PDF stored.
                mode["cancel"] = "active"
                download_count = len(downloads)
                rebuild = page.locator('#detail .pdf-action [data-regenerate="true"]')
                rebuild.click()
                progress = page.locator("#report-progress")
                progress.get_by_role("button", name="Остановить подготовку").click()
                expect(progress).to_contain_text("Останавливаем после текущего запроса")
                assert cancel_requests == ["POST"]
                mode["cancel"] = "cancelled"
                page.clock.fast_forward(1600)
                expect(progress).to_contain_text("Подготовка остановлена")
                expect(rebuild).to_be_enabled()
                assert len(downloads) == download_count and mode["latest"] == "retried"
                del mode["cancel"]
                # Historical scan IDs and direct links both reach the same canonical card.
                page.locator('[data-action="close-detail"]').click()
                page.evaluate("chooseDataset('web',['historical-alias','imported-canonical'])")
                imported = page.locator('[data-card="imported-canonical"]')
                expect(imported).to_contain_text("Единая карточка из импорта")
                expect(imported).to_have_count(1)
                expect(page.locator('[data-card="historical-alias"]')).to_have_count(0)
                assert page.evaluate("state.runIds") == ["imported-canonical"]
                imported_dataset = page.evaluate(
                    "state.records.find(r=>r.id==='imported-canonical').dataset"
                )
                assert imported_dataset == "imported"
                page.evaluate("openRecord('historical-alias')")
                expect(page.locator("#detail-title")).to_have_text("Единая карточка из импорта")
                expect(page.locator('#detail .pdf-action [data-pdf]').first).to_have_attribute(
                    "data-pdf", "imported-canonical"
                )
                assert report_task_requests[-1] == "/api/records/imported-canonical/report-task"
                page.locator('[data-action="close-detail"]').click()
                page.locator('[data-action="all-records"]').click()
                expect(imported).to_have_count(0)
                # A slow extra response must not leak into a newly selected dataset.
                page.evaluate("""() => {
                  const previous=window.fetch; window.extraFetchStarted=false;
                  window.fetch=async (...args)=>{
                    if(String(args[0])==='/api/records/imported-canonical'){
                      window.extraFetchStarted=true;
                      await new Promise(resolve=>setTimeout(resolve,600));
                    }
                    return previous(...args);
                  };
                  void chooseDataset('web',['imported-canonical']);
                }""")
                page.wait_for_function("() => window.extraFetchStarted")
                page.evaluate("chooseDataset('demo')")
                page.wait_for_timeout(750)
                expect(imported).to_have_count(0)
                expect(page.locator("#dataset")).to_have_value("demo")
                assert not errors, errors
                browser.close()
                print(
                    "Old-format warning, explicit V8, progress, download errors, report retry, "
                    "report cancellation, merged card links and chat retry/history passed"
                )
        finally:
            server.terminate()
            server.wait(timeout=10)
