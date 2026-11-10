"""Two-page evidence-aware brief; fit full paragraphs, never clip source text."""

from datetime import datetime
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Paragraph


def render_pdf(brief: dict) -> bytes:
    font = Path(__file__).parent / "fonts/DejaVuSans.ttf"
    if "Radar" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("Radar", str(font)))
    width, height = A4
    ink, muted, accent = "#26333e", "#73808a", "#173e54"
    pages: list[list[tuple]] = [[], []]

    def add(page, text, size=9.5, color=ink, gap=10, url=None):
        pages[page].append((str(text), size, color, gap, url))

    def heading(page, text):
        add(page, text, 11, accent, 8)

    add(0, brief["name"], 18, accent)
    add(0, f"{brief['developer']} · {brief['region']}", 9, muted)
    score = brief.get("assessment")
    if score:
        add(
            0,
            f"Острота: {score['severity']}   ·   Продаваемость: {score['sellability']}   ·   "
            f"Окно: {score['window']}   ·   Класс: {score['priority']} (демо)",
            9,
        )
    else:
        add(0, "Оценка и класс: не рассчитаны · корпус и сроки требуют подтверждения", 8, muted)
    heading(0, "Что известно")
    add(0, brief["summary"])
    stats = brief.get("chat_stats")
    heading(0, "Обсуждения жителей")
    if stats:
        negative = stats["negative_share"]
        add(
            0,
            f"За 30 дней: {stats['messages_30d']} сообщений, {stats['authors_30d']} авторов. "
            + (
                f"Негативных среди размеченных: {negative}%."
                if negative is not None
                else "Тональность не размечена."
            ),
            9,
        )
        add(
            0,
            "Темы: " + "; ".join(f"{t.replace('_', ' ')} — {n}" for t, n in stats["topics"][:4]),
            9,
        )
        add(
            0,
            "По локальной выгрузке. Темы и тональность заданы в ней; "
            "отрицательная тональность не равна доле конфликта.",
            7.5,
            muted,
        )
    else:
        add(0, "Переписка не добавлена. Боли определяются только по исходным материалам.", 9)
    if brief["evidence"]:
        heading(0, "Доказательная база — требует сверки с оригиналом")
        for ev in brief["evidence"][:2]:
            add(0, f"«{ev['quote']}» — {ev['title']}", 8.5, url=ev["url"])
        if len(brief["evidence"]) > 2:
            add(0, f"Ещё {len(brief['evidence']) - 2} цитат в карточке приложения.", 8, muted)
    if brief.get("note"):
        heading(0, "Уточнение аналитика")
        add(0, brief["note"], 9)
    heading(0, "Пробелы и ближайшая проверка")
    add(0, brief["check"], 9)
    add(0, "Хроника и ближайшие даты: структурированные сведения пока не подтверждены.", 8, muted)
    add(0, brief["limitation"], 8, muted)
    add(0, f"Происхождение: {brief['file']} / {brief['sheet']} / строка {brief['row']}", 7.5, muted)

    main = brief["offers"][0] if brief["offers"] else None
    if main:
        add(1, main["name"], 18, accent)
        add(1, main["promise"], 11)
        heading(1, "Почему это предложение")
        selected_topics = [t.replace("_", " ") for t in brief.get("topics", [])]
        reason = (
            "Темы для проработки: " + ", ".join(selected_topics[:5]) + ". "
            if selected_topics
            else ""
        )
        if main["code"] == "O1":
            reason += (
                "Сначала необходимо закрыть пробелы в доказательствах и определить масштаб задачи."
            )
        else:
            reason += "Пакет выбран по указанным темам, типу ситуации и стадии конфликта."
        add(1, reason, 9)
        heading(1, "Что предлагаем сделать")
        for item in main["deliverables"]:
            add(1, "• " + item, 9.5, gap=5)
        heading(1, "Пилот на 30 дней")
        add(1, f"Объект пилота: {brief['name']}. " + main["pilot_30d"], 9)
        add(1, "Показатели: " + "; ".join(main["kpis"]), 9)
        add(1, "Кому предложить: " + "; ".join(main["buyer_roles"]), 9)
    for offer in brief["offers"][1:]:
        add(1, "Дополнение: " + offer["name"], 9)
    heading(1, "Перед коммерческим предложением")
    add(
        1,
        "1. Подтвердить корпус, даты и основания каждого вывода.\n"
        "2. Определить уполномоченного заказчика и проверить стоп-лист.\n"
        "3. Согласовать с агентством объём пилота и формулировки услуг.",
        9,
    )
    heading(1, "Источники")
    for i, url in enumerate(brief["urls"][:3], 1):
        add(1, f"[{i}] Открыть первоисточник", 8.5, accent, 5, url)
    if not brief["urls"]:
        add(1, "Демо-пример: реальных источников нет.", 8, muted)
    if len(brief["urls"]) > 3:
        add(1, f"Ещё {len(brief['urls']) - 3} ссылок в карточке приложения.", 8, muted)
    add(
        1,
        "Предложение — рабочая гипотеза. Пилот и KPI требуют вычитки агентством. "
        "Проверка фактов второй моделью и внешняя отправка не выполнялись.",
        8,
        muted,
    )

    stream = BytesIO()
    canvas = Canvas(stream, pagesize=A4)
    canvas.setTitle("Мегаполис Интеледженс — разбор объекта")
    for number, blocks in enumerate(pages, 1):
        prepared = []
        for scale in [1, 0.95, 0.9, 0.85, 0.8]:
            prepared = []
            for text, size, color, gap, url in blocks:
                style = ParagraphStyle(
                    "p",
                    fontName="Radar",
                    fontSize=size * scale,
                    leading=size * scale * 1.4,
                    textColor=colors.HexColor(color),
                )
                p = Paragraph(escape(text).replace("\n", "<br/>"), style)
                _, h = p.wrap(width - 80, height)
                prepared.append((p, h, gap * scale, url))
            if sum(h + gap for _, h, gap, _ in prepared) <= height - 158:
                break
        else:
            raise ValueError(
                "Разбор не помещается на две страницы. Сократите уточнение или цитаты."
            )
        canvas.setFillColor(colors.HexColor(accent))
        canvas.rect(0, height - 73, width, 73, fill=1, stroke=0)
        canvas.setFillColor(colors.white)
        canvas.setFont("Radar", 10)
        canvas.drawString(40, height - 29, "МЕГАПОЛИС ИНТЕЛЕДЖЕНС")
        canvas.setFont("Radar", 15)
        canvas.drawString(40, height - 54, "Что происходит" if number == 1 else "С чем идти")
        canvas.setFont("Radar", 8)
        version = f"v{brief['version']}" if brief.get("version") else "Черновик"
        canvas.drawRightString(width - 40, height - 30, version)
        stamp = "ДЕМО" if brief["dataset"] == "demo" else "ТРЕБУЕТ ПРОВЕРКИ"
        canvas.drawRightString(width - 40, height - 51, stamp)
        y = height - 96
        for p, h, gap, url in prepared:
            p.drawOn(canvas, 40, y - h)
            if url:
                canvas.linkURL(url, (40, y - h, width - 40, y), relative=0)
            y -= h + gap
        canvas.setFillColor(colors.HexColor(muted))
        canvas.setFont("Radar", 7)
        created = brief.get("created_at")
        date_label = (
            (
                datetime.fromisoformat(created)
                .astimezone(ZoneInfo("Europe/Moscow"))
                .strftime("%d.%m.%Y %H:%M МСК")
            )
            if created
            else ""
        )
        canvas.drawString(40, 29, "Внутренний черновик · не пересылать · " + date_label)
        canvas.drawRightString(width - 40, 29, f"{number}/2")
        canvas.showPage()
    canvas.save()
    return stream.getvalue()
