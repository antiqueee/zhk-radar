"""Allowlisted client proposal: no internal scores, notes, chat quotes or raw source text."""

import hashlib
import json
import re
from io import BytesIO
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import KeepTogether, SimpleDocTemplate, Spacer

from radar.briefs.report import PLAYBOOKS
from radar.web import cases, review, stoplist
from radar.web.brief_service import payload_for
from radar.web.chats import redact
from radar.web.pdf import MUTED, NAVY, Route, p


def current(store, brief):
    snapshot = payload_for(store, brief["record_id"])
    fingerprint = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    expected = brief["payload"].get("author", {}).get("base_fingerprint", brief["fingerprint"])
    current_cases = {c["id"]: c for c in cases.entries(store)}
    selected = {i for p in brief["payload"]["report"]["products"] for i in p.get("case_ids", [])}
    case_changed = any(
        c["id"] in selected and current_cases.get(c["id"]) != c
        for c in brief["payload"].get("cases", [])
    )
    return fingerprint == expected and not case_changed


def overlaps(text, quotes):
    words = re.findall(r"\w+", text.casefold())
    grams = {" ".join(words[i : i + 7]) for i in range(max(0, len(words) - 6))}
    return any(
        grams.intersection(" ".join(w[i : i + 7]) for i in range(max(0, len(w) - 6)))
        for w in [re.findall(r"\w+", q.casefold()) for q in quotes]
    )


def projection(payload, allowed_cases):
    chat = payload.get("chat_stats") or {}
    forbidden = [e["text"] for e in chat.get("examples", [])]
    products = []
    selected_cases = {i for p in payload["report"]["products"] for i in p.get("case_ids", [])}
    for product in payload["report"]["products"]:
        tasks, result, measurement, _ = PLAYBOOKS[product["code"]]
        proposed = [*product["tasks"], product["result"], product["measurement"]]
        # Authored text may quote a resident despite instructions. Use vetted playbook then.
        if not any(overlaps(t, forbidden) or redact(t, []) != t for t in proposed):
            tasks, result, measurement = product["tasks"], product["result"], product["measurement"]
        products.append(
            {
                "name": product["name"],
                "topics": [t.replace("_", " ") for t in product["matching_topics"]],
                "tasks": tasks,
                "result": result,
                "measurement": measurement,
            }
        )
    return {
        "name": redact(payload["name"], []),
        "building": redact(payload.get("building") or "", []),
        "developer": redact(payload["developer"], []),
        "products": products,
        "cases": [
            {"title": c["title"], "description": c["description"], "result": c["result"]}
            for c in payload.get("cases", [])
            if c["id"] in allowed_cases & selected_cases and c["client_use"]
        ],
        "synthetic": payload["dataset"] == "demo",
    }


def render_client(value, version=1, draft=True):
    base = Path(__file__).parent
    if "Radar" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("Radar", str(base / "fonts/DejaVuSans.ttf")))
    stream = BytesIO()
    document = SimpleDocTemplate(
        stream,
        pagesize=A4,
        leftMargin=44,
        rightMargin=44,
        topMargin=100,
        bottomMargin=60,
        title="Мегаполис — предложение для обсуждения",
    )

    def heading(text, size=11):
        item = p(text, size, NAVY)
        item.keepWithNext = True
        return item

    story = [
        p("Предложение для обсуждения", 23, NAVY, 16),
        p(value["name"], 16, NAVY),
        p(value["building"] or "Границы проекта уточняются на первой встрече", 10, MUTED),
        p(
            "Предлагаем пилот по наиболее болезненному для покупателей вопросу: "
            "наладить предметный диалог и довести согласованные действия до результата. "
            "Состав программы и ресурсы согласуем с вашей командой."
        ),
        Route(
            [
                "Выбрать задачу и аудиторию",
                "Согласовать план и ответственных",
                "Провести пилот и оценить результат",
            ]
        ),
        Spacer(1, 16),
    ]
    for index, product in enumerate(value["products"], 1):
        block = [
            heading(f"{index}. {product['name']}", 15),
            p(
                "Фокус обсуждения: "
                + (", ".join(product["topics"]) or "коммуникации по объекту")
                + ". Применимость этих тем подлежит совместной проверке.",
                9,
                MUTED,
            ),
        ]
        for i, task in enumerate(product["tasks"], 1):
            block.append(p(f"{i:02d}. {task}", 10))
        block.extend(
            [
                heading("Результат пилота"),
                p(product["result"]),
                heading("Как оценим работу"),
                p(product["measurement"], 9),
                Spacer(1, 12),
            ]
        )
        story.append(KeepTogether(block))
    for case in value["cases"]:
        story.extend(
            [
                p("Релевантный опыт: " + case["title"], 13, NAVY),
                p(case["description"]),
                p(case["result"]),
                p("Результат другого проекта не гарантирует такой же результат здесь.", 9, MUTED),
            ]
        )
    story.extend(
        [
            p("Первые 30 дней", 15, NAVY),
            p(
                "Дни 1–5: согласовать границы, доступные материалы и исходные показатели. "
                "Дни 6–15: подготовить решения, назначить ответственных и запустить пилот. "
                "Дни 16–30: проверить результат и согласовать следующий этап."
            ),
            p("На встрече согласуем", 12, NAVY),
            p(
                "Приоритетную задачу, ответственного с вашей стороны, состав материалов, "
                "объём работ, сроки и стоимость. Числовые цели определяются после исходного замера."
            ),
            p("Мегаполис Интеледженс · центр развития коммуникативных технологий", 9, MUTED),
        ]
    )

    def page(canvas, doc):
        canvas.saveState()
        x, y, size = 44, A4[1] - 72, 40
        clip = canvas.beginPath()
        clip.rect(x, y, size, size)
        canvas.clipPath(clip, stroke=0)
        scale = size / 180
        canvas.drawImage(
            str(base / "static/megapolis-logo.jpg"),
            x - 245 * scale,
            y - (640 - 330) * scale,
            640 * scale,
            640 * scale,
        )
        canvas.restoreState()
        canvas.saveState()
        canvas.setFillColor(NAVY)
        canvas.setFont("Radar", 12)
        canvas.drawString(97, A4[1] - 48, "МЕГАПОЛИС")
        canvas.setFont("Radar", 8)
        canvas.drawString(97, A4[1] - 64, "ИНТЕЛЕДЖЕНС")
        canvas.setFillColor(MUTED)
        canvas.setFont("Radar", 7)
        label = (
            "УЧЕБНЫЙ ПРИМЕР"
            if value["synthetic"]
            else ("ЧЕРНОВИК · НЕ ДЛЯ ОТПРАВКИ" if draft else "ПРЕДЛОЖЕНИЕ ДЛЯ ОБСУЖДЕНИЯ")
        )
        canvas.drawRightString(A4[0] - 44, A4[1] - 48, label)
        canvas.drawString(44, 28, f"Версия {version}")
        canvas.drawRightString(A4[0] - 44, 28, f"Мегаполис Интеледженс · {doc.page}")
        canvas.restoreState()

    document.build(story, onFirstPage=page, onLaterPages=page)
    return stream.getvalue()


def export(store, ident, draft=True):
    brief = review.get_brief(store, ident)
    if stoplist.blocked(store, store.get(brief["record_id"])):
        raise ValueError("Объект в стоп-листе")
    if not draft:
        if not review.operator_approved(store, ident):
            raise ValueError("Сначала аналитик должен одобрить именно эту версию")
        if not current(store, brief):
            raise ValueError("Данные изменились. Обновите отчёт и проверьте новую версию")
    allowed = {c["id"] for c in cases.entries(store) if c["approved"] and c["client_use"]}
    return render_client(projection(brief["payload"], allowed), brief["version"], draft)
