"""Two-page decision brief. Evidence and proposed work occupy separate pages."""

import re
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import KeepInFrame, Paragraph, Spacer

from radar.web.pdf import INK, MUTED, NAVY, PALE, Route, clean, p


def excerpt(value, limit):
    value = re.sub(r"\s+", " ", clean(value)).strip()
    if len(value) <= limit:
        return value
    sentences = list(re.finditer(r"[.!?](?:\s|$)", value[:limit]))
    if sentences and sentences[-1].start() > limit / 3:
        return value[: sentences[-1].start() + 1]
    # Explicit excerpts, never an invented summary or hidden clipping.
    return value[:limit].rsplit(" ", 1)[0].rstrip(".,:;") + "…"


def render(brief):
    base = Path(__file__).parent
    if "Radar" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("Radar", str(base / "fonts/DejaVuSans.ttf")))
    stream = BytesIO()
    canvas = Canvas(stream, pagesize=A4)
    canvas.setTitle("Мегаполис · " + brief["name"])
    canvas.setAuthor("Мегаполис Интеледженс")
    width, height = A4
    content_width = width - 88
    research = brief.get("dossier") or {}
    signals = research.get("signals", [])
    answers = (brief.get("followup") or {}).get("answers", [])
    chats = brief.get("chat_stats") or {}
    examples = chats.get("examples", [])
    urls = list(
        dict.fromkeys(
            [s["source_url"] for s in signals]
            + [e["url"] for e in examples[:2] if e.get("url")]
            + [a["url"] for a in answers]
            + brief.get("urls", [])
        )
    )
    references = {url: i + 1 for i, url in enumerate(urls)}
    products = (brief.get("report") or {}).get("products", brief.get("offers", []))[:3]

    def readable(value):
        for item in brief.get("evidence", []):
            if "id" in item:
                value = value.replace(
                    "evidence:" + str(item["id"]),
                    "[" + str(references.get(item.get("url"), "источник")) + "]",
                )
        value = re.sub(r"chat:\d+", "[Чат ЖК]", value)
        value = re.sub(r"\[Чат ЖК\](?:, \[Чат ЖК\])+", "[Чат ЖК]", value)
        value = re.sub(r"\b[a-f0-9]{64}\b", "публичный источник", value)
        return value

    def heading(text):
        return p(text, 11, NAVY, 5)

    def body(text, size=9.5, color=INK, space=7):
        return p(readable(text), size, color, space)

    def frame(page, label, story):
        canvas.setFillColor(NAVY)
        canvas.rect(0, height - 8, width, 8, fill=1, stroke=0)
        logo = base / "static/megapolis-logo.jpg"
        if logo.exists():
            canvas.drawImage(
                str(logo),
                37,
                height - 73,
                width=62,
                height=62,
                preserveAspectRatio=True,
                mask="auto",
            )
        canvas.setFont("Radar", 11)
        canvas.drawString(105, height - 39, "МЕГАПОЛИС ИНТЕЛЕДЖЕНС")
        canvas.setFillColor(MUTED)
        canvas.setFont("Radar", 8)
        canvas.drawString(105, height - 55, label)
        box = KeepInFrame(content_width, height - 140, story, mode="shrink")
        box.canv = canvas
        _, h = box.wrap(content_width, height - 140)
        # KeepInFrame guarantees two pages without dropping overflowing flowables.
        box.drawOn(canvas, 44, height - 85 - h)
        canvas.setStrokeColor(PALE)
        canvas.line(44, 40, width - 44, 40)
        canvas.setFont("Radar", 7)
        canvas.setFillColor(MUTED)
        status = (
            "ДЕМО · вымышленный пример"
            if brief.get("dataset") == "demo"
            else "Внутренний разбор · выводы требуют проверки"
        )
        canvas.drawString(44, 27, status)
        canvas.drawRightString(width - 44, 27, f"{page} / 2")
        canvas.showPage()

    page = [
        p(excerpt(brief["name"], 110), 21, NAVY, 6),
        body(
            " · ".join(
                filter(None, [brief.get("developer"), brief.get("region"), brief.get("building")])
            ),
            9,
            MUTED,
            13,
        ),
        heading("01  Что происходит"),
        body(excerpt(brief.get("summary", "Нет подтверждённых сведений"), 700)),
    ]
    page.append(heading("На чём основан вывод"))
    if signals:
        for signal in signals[:4]:
            stamp = signal.get("event_date") or signal.get("published_at") or "дата не установлена"
            number = references.get(signal["source_url"], "")
            page.append(
                body(
                    f"[{number}] {stamp} · {signal.get('kind_label', 'Сигнал источника')}",
                    8,
                    MUTED,
                    2,
                )
            )
            page.append(
                body("«" + excerpt(signal.get("quote", signal["summary"]), 280) + "»", space=6)
            )
    else:
        page.append(body("Прямые цитаты пока не собраны. Исходная карточка требует проверки."))
    reg = brief.get("registry") or {}
    if reg.get("binding", {}).get("confirmed") if reg.get("binding") else False:
        snapshots = reg.get("snapshots", [])
        if snapshots:
            last = snapshots[-1]
            page.append(heading("РЕЕСТР КОРПУСА"))
            page.append(
                body(
                    f"Ввод: {last.get('commissioning') or 'нет данных'}. "
                    f"Передача: {last.get('transfer') or 'нет данных'}. "
                    + (
                        f"РВЭ: {last.get('rve')}."
                        if last.get("rve_checked")
                        else "РВЭ: Наличие не проверено."
                    )
                    + (" Снимок загружен оператором." if last.get("imported") else ""),
                    8.5,
                )
            )
    topics = set(brief.get("topics", []))
    if signals:
        labels = (
            ["Инженерная претензия", "Риск повторных жалоб", "Проверить порядок устранения"]
            if topics & {"протечки", "качество_ук"}
            else [
                "Сигналы о сроках и сервисе",
                "Риск потери доверия",
                "Проверить ответы покупателям",
            ]
        )
        page += [heading("Рабочая гипотеза для проверки"), Route(labels), Spacer(1, 12)]
    official = [a for a in answers if a["question"] == "developer_response"]
    page += [
        heading("Позиция компании и пробелы проверки"),
        body(
            "[" + str(references[official[0]["url"]]) + "] " + excerpt(official[0]["quote"], 300)
            if official
            else "Прямая позиция застройщика по этой проблеме в материалах не найдена."
        ),
        body(
            excerpt(brief.get("check", "Уточнить актуальность и масштаб проблемы."), 220), 9, MUTED
        ),
    ]
    page.append(heading("Что добавили открытые чаты"))
    if chats.get("messages_30d"):
        topics = "; ".join(
            t.replace("_", " ") + f": {n}"
            for t, n in [(t, n) for t, n in chats.get("topics", []) if t != "не_размечено"][:3]
        )
        page.append(
            body(
                f"За 30 дней: {chats['messages_30d']} сообщений, "
                f"{chats.get('authors_30d', 0)} авторов. "
                f"Размечено моделью: {chats.get('model_labelled', 0)}. "
                + ("Темы размеченной выборки: " + topics + ". " if topics else "")
                + "Выборка не отражает мнение всех покупателей."
            )
        )
        for example in examples[:2]:
            label = f"[{references[example['url']]}] " if example.get("url") else ""
            page.append(body(label + "«" + excerpt(example["text"], 150) + "»", 8.5))
    else:
        page.append(
            body("История чатов не прочитана. Выводов о настроениях участников нет.", color=MUTED)
        )
    page.append(heading("Источники"))
    for url in urls[:6]:
        if urlsplit(url).scheme not in {"http", "https"}:
            continue
        label = f"[{references[url]}] {urlsplit(url).hostname} · открыть материал"
        paragraph = body(label, 8, MUTED, 3)
        page.append(
            Paragraph(
                f'<link href="{escape(url, {chr(34): "&quot;"})}">{escape(label)}</link>',
                paragraph.style,
            )
        )
    if len(urls) > 6 or len(signals) > 4:
        page.append(
            body(
                "Здесь ключевые сигналы; полный архив источников — в карточке ЖК.",
                8,
                MUTED,
            )
        )
    page.append(
        body(
            "Перенос ввода и задержка ключей — разные события. Масштаб и спрос не установлены.",
            8,
            MUTED,
        )
    )
    frame(1, "СИТУАЦИЯ И ОСНОВАНИЯ", page)

    page = [
        p("Что предложить девелоперу", 21, NAVY, 8),
        body(excerpt(brief["name"], 110), 10, MUTED, 12),
    ]
    for i, product in enumerate(products, 1):
        page.append(heading(f"0{i}  {product['name']}"))
        rationale = product.get("rationale")
        if rationale:
            page.append(body(excerpt(rationale, 260)))
        else:
            page.append(
                body(
                    "Основание для проверки: «"
                    + excerpt(product.get("case_trigger", brief.get("summary")), 170)
                    + "»."
                )
            )
        tasks = product.get("tasks", [])[:3]
        if tasks:
            page.append(
                body("Действия: " + "; ".join(excerpt(t, 130).rstrip(".;") for t in tasks) + ".", 9)
            )
        page.append(
            body(
                "Результат: "
                + excerpt(
                    product.get("result", "Согласованный план работы по выявленной проблеме."), 180
                ),
                9,
            )
        )
        page.append(
            body(
                "Как измерить: "
                + excerpt(
                    product.get("measurement", "Согласовать исходный уровень и повторный замер."),
                    140,
                ),
                8.5,
                MUTED,
                12,
            )
        )
    if not products:
        page.append(
            body("Первый шаг — диагностика корпуса и обращений. Для адаптации услуги мало данных.")
        )
    page += [
        Spacer(1, 4),
        Route(
            [
                "Проверка фактов\nСроки и обращения",
                "Пилот на 30 дней\nОтветы и эскалация",
                "Замер эффекта\nПовторные обращения",
            ]
        ),
        Spacer(1, 12),
        heading("Как начать разговор"),
        body(
            "Предложить клиентскому сервису и PR проверку сигналов на одном ЖК. "
            "Согласовать факты, ответственных и план работы. Расширение — после пилота.",
            9,
        ),
        body(
            "Объём работ и показатели согласуются с заказчиком. Спрос на подрядчика не установлен.",
            8,
            MUTED,
        ),
    ]
    frame(2, "ПРЕДЛОЖЕНИЕ И ПЕРВЫЙ ШАГ", page)
    canvas.save()
    return stream.getvalue()
