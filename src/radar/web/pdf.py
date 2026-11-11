"""Branded, paginated evidence and service report. No fixed page-count or clipping."""

import re
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Flowable,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

INK = colors.HexColor("#22333f")
NAVY = colors.HexColor("#173e54")
MUTED = colors.HexColor("#637580")
PALE = colors.HexColor("#eef3f5")
WIDTH = A4[0] - 88


def clean(value):
    return str(value or "").replace("\u2011", "-").replace("\u2013", "-").replace("\u2014", "-")


def p(text, size=10, color=INK, space=9):
    return Paragraph(
        escape(clean(text)).replace("\n", "<br/>"),
        ParagraphStyle(
            "body",
            fontName="Radar",
            fontSize=size,
            leading=size * 1.4,
            textColor=color,
            spaceAfter=space,
            splitLongWords=True,
        ),
    )


class Route(Flowable):
    """A small vector diagram; labels wrap and height follows content."""

    def __init__(self, labels):
        super().__init__()
        self.labels = labels

    def wrap(self, width, height):
        self.width = width
        self.cell = (width - 32) / 3
        self.paragraphs = [p(label, 9, NAVY, 0) for label in self.labels]
        self.heights = [paragraph.wrap(self.cell - 20, 1000)[1] for paragraph in self.paragraphs]
        self.height = max(self.heights) + 30
        return width, self.height

    def draw(self):
        c = self.canv
        for i, (paragraph, h) in enumerate(zip(self.paragraphs, self.heights, strict=True)):
            x = i * (self.cell + 16)
            c.setFillColor(PALE)
            c.roundRect(x, 0, self.cell, self.height, 5, fill=1, stroke=0)
            paragraph.drawOn(c, x + 10, self.height - 15 - h)
            if i < 2:
                c.setStrokeColor(NAVY)
                y = self.height / 2
                c.line(x + self.cell + 3, y, x + self.cell + 13, y)
                c.line(x + self.cell + 10, y + 3, x + self.cell + 13, y)
                c.line(x + self.cell + 10, y - 3, x + self.cell + 13, y)


def render_pdf(brief: dict) -> bytes:
    brief = dict(brief)
    if not brief.get("building"):
        match = re.search(r"(?:корпус|дом)\s+[\w./-]+", brief["name"], re.I)
        if match:
            brief["building"] = match[0]
    base = Path(__file__).parent
    if "Radar" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("Radar", str(base / "fonts/DejaVuSans.ttf")))
    report = brief.get("report")
    if not report:
        # Historical versions remain printable without mutating their saved content.
        from radar.briefs.report import PLAYBOOKS

        products = []
        for o in brief.get("offers", []):
            tasks, result, measurement, dependency = PLAYBOOKS[o["code"]]
            products.append(
                {
                    **o,
                    "tasks": tasks,
                    "result": result,
                    "measurement": measurement,
                    "dependency": dependency,
                    "matching_topics": [],
                    "evidence_urls": [],
                    "scope": brief["name"],
                    "starting_question": brief.get("check", ""),
                }
            )
        report = {"products": products}
    stream = BytesIO()
    doc = SimpleDocTemplate(
        stream,
        pagesize=A4,
        leftMargin=44,
        rightMargin=44,
        topMargin=100,
        bottomMargin=52,
        title=f"Мегаполис · {brief['name']}",
        author="Мегаполис Интеледженс",
        allowSplitting=True,
    )
    story = []
    research = brief.get("dossier", {})
    signals = research.get("signals", [])
    urls = list(dict.fromkeys([*brief.get("urls", []), *[s["source_url"] for s in signals]]))
    numbers = {url: i for i, url in enumerate(urls, 1)}
    products = report["products"]

    def add(text, **kwargs):
        story.append(p(text, **kwargs))

    def title(number, label, subtitle=None):
        add(f"{str(number).zfill(2)}  /  {label.upper()}", size=9, color=MUTED, space=16)
        if subtitle:
            add(subtitle, size=23, color=NAVY, space=18)

    def section(label, text):
        story.append(KeepTogether([p(label, 11, NAVY, 6), p(text)]))

    def link(label, url):
        if urlsplit(url).scheme not in {"http", "https"}:
            add(label)
            return
        style = ParagraphStyle(
            "link", fontName="Radar", fontSize=9, leading=14, textColor=NAVY, spaceAfter=8
        )
        story.append(
            Paragraph(
                f'<link href="{escape(url, {chr(34): "&quot;"})}" color="#173e54">'
                f"{escape(clean(label))}</link>",
                style,
            )
        )

    title(1, "Решение для объекта", brief["name"])
    add(f"{brief.get('developer', '')} · {brief.get('region', '')}", size=12, color=MUTED)
    add(brief.get("building") or "Границы корпуса уточняются перед началом работы", color=MUTED)
    story.append(Spacer(1, 10))
    section("Исходная ситуация", brief.get("summary", "Сведения пока не добавлены"))
    story.append(
        Route(
            [
                "Собрать и проверить\nФакты, даты, источники",
                "Спроектировать ответ\nПродукты под задачи ЖК",
                "Проверить результат\nДействия, сроки, показатели",
            ]
        )
    )
    story.append(Spacer(1, 20))
    section(
        "Предлагаемый подход",
        "Начать с проверки существенных утверждений и границ задачи. "
        "Подключать коммуникационные продукты после подтверждения соответствующих проблем. "
        "Ниже — проект работ для обсуждения с агентством и заказчиком.",
    )
    add("В этом разборе", size=11, color=NAVY)
    for i, product in enumerate(products, 1):
        add(f"{i}. {product['name']}", space=5)
    story.append(Spacer(1, 10))
    add(brief.get("limitation", "Выводы требуют проверки по первоисточникам."), size=9, color=MUTED)
    add(
        "Продукты и показатели — проектная адаптация услуг Мегаполиса, не утверждённый прайс. "
        "Документ подготовлен по сохранённым данным; его создание не запускает новый поиск.",
        size=9,
        color=MUTED,
    )

    if brief.get("author"):
        add(
            "Подготовлено моделью; проверка: "
            + (
                "опора на источники рассмотрена"
                if brief["author"]["status"] == "reviewed"
                else "требуется ручной разбор"
            ),
            size=9,
            color=MUTED,
        )
    story.append(PageBreak())
    title(2, "Основания и пробелы", "Что известно о ситуации")
    section("Сведения из материалов", brief.get("summary", "Нет сведений"))
    section(
        "Что пока нельзя утверждать",
        "Публикация или сообщение жителя подтверждают наличие "
        "утверждения, но сами по себе не доказывают нарушение. Масштаб проблемы, актуальность "
        "сроков и применимость к конкретному корпусу требуют отдельной проверки.",
    )
    section(
        "Ближайшая проверка именно по этому объекту", brief.get("check", "Уточнить корпус и даты")
    )
    if brief.get("note"):
        section("Уточнение аналитика", brief["note"])
    add("Хронология сигналов", size=13, color=NAVY)
    if not signals:
        add(
            "Структурированная хроника отсутствует. Даты событий не восстановлены из предположений."
        )
    for s in signals:
        stamp = s.get("event_date") or (
            f"{s['published_at']} · дата публикации"
            if s.get("published_at")
            else "Дата события не установлена"
        )
        add(
            f"{stamp} · {s.get('kind_label', 'Сигнал')} [{numbers.get(s['source_url'], '?')}]",
            color=NAVY,
        )
        add(s.get("summary") or s.get("quote") or "См. сохранённый источник", size=9)
    stats = brief.get("chat_stats")
    add("Обсуждения жителей", size=13, color=NAVY)
    if stats:
        add(
            f"За 30 дней: {stats['messages_30d']} сообщений и {stats['authors_30d']} авторов. "
            "Это охват подключённых источников, а не всех жителей ЖК."
        )
        for topic, count in stats.get("topics", []):
            add(f"{topic.replace('_', ' ')}: {count} сообщений", space=3)
        neg = stats.get("negative_share")
        add(
            f"Негативных среди размеченных: {neg}%."
            if neg is not None
            else "Тональность не размечена.",
            size=9,
        )
        add(
            f"Моделью размечено сообщений: {stats.get('model_labelled', 0)}. "
            "Разметка требует проверки. Сообщения без разметки учитываются в объёме, "
            "но не в оценке тональности.",
            size=9,
            color=MUTED,
        )
    else:
        add("Открытые чаты пока не добавлены. Частота жалоб и динамика обсуждения неизвестны.")

    registry = brief.get("registry") or {}
    if registry.get("binding"):
        story.append(PageBreak())
        title("2А", "Реестр корпуса", "Даты, наблюдения и границы расчёта")
        binding = registry["binding"]
        section(
            "Привязка",
            "ID корпуса ЕИСЖС: "
            + binding["house_id"]
            + (
                ". Соответствие адресу подтверждено оператором."
                if binding["confirmed"]
                else ". Соответствие адресу ещё не подтверждено."
            ),
        )
        snapshots = registry.get("snapshots", [])
        if snapshots:
            last = snapshots[-1]
            observed = (
                datetime.fromisoformat(last["observed_at"])
                .astimezone(ZoneInfo("Europe/Moscow"))
                .strftime("%d.%m.%Y %H:%M МСК")
            )
            section("Последний сохранённый снимок", last["name"] + " · " + observed)
            for key, label in [
                ("commissioning", "Плановый ввод"),
                ("transfer", "Передача"),
                ("readiness", "Готовность, %"),
                ("rve", "РВЭ"),
            ]:
                value = last.get(key)
                if key == "rve" and not last.get("rve_checked"):
                    value = "Наличие не проверено"
                add(label + ": " + (str(value) if value is not None else "Нет данных"))
            add("Источник: " + last["source_url"], size=9)
            add(
                "Загружено оператором; подлинность не проверена автоматически."
                if last["imported"]
                else "Получено через адаптер реестра.",
                size=9,
                color=MUTED,
            )
        else:
            add("Снимков пока нет. Привязка ID не заменяет реестровые данные.")
        changes = registry.get("changes", [])
        section(
            "Изменения между снимками",
            "Показаны последние десять изменений. "
            "Первый сохранённый срок не обязательно совпадает с первоначальным ДДУ.",
        )
        for change in changes[-10:]:
            label = {
                "commissioning": "Ввод",
                "transfer": "Передача",
                "readiness": "Готовность",
                "rve": "РВЭ",
            }[change["field"]]
            add(f"{label}: {change['before']} → {change['after']}", size=9)
        if not changes:
            add("Изменений между сопоставимыми заполненными полями пока не зафиксировано.")
        score = registry.get("assessment")
        if score:
            priority = {"early": "ранний риск", "watch": "наблюдение", "none": "не определён"}.get(
                score["priority"], score["priority"]
            )
            certainty = {"high": "высокая", "medium": "средняя", "low": "низкая"}[
                score["confidence"]
            ]
            section(
                "Предварительная оценка",
                f"Приоритет: {priority}. "
                f"Уверенность: {certainty}. "
                "Это расчёт по доступным снимкам и введённому коммерческому контексту.",
            )
            for limitation in score["limitations"]:
                add(limitation, size=9, color=MUTED)

    story.append(PageBreak())
    title(3, "Карта работы", "От сигнала к предложению")
    add(
        "Связи ниже — рабочие гипотезы. Их следует подтвердить до коммерческого предложения. "
        "Один продукт может закрывать несколько связанных вопросов; общий сбор "
        "фактов выполняется один раз."
    )
    for product in products:
        topic = (
            ", ".join(t.replace("_", " ") for t in product["matching_topics"])
            or "Неполнота проверенных данных"
        )
        add(product["name"], size=13, color=NAVY)
        story.append(Route([topic, product["promise"], product["deliverables"][0]]))
        story.append(Spacer(1, 14))
        add("Условие подключения: " + product["dependency"], size=9)
    section(
        "Как сочетать продукты",
        "Диагностика формирует общий факт-лист. Профильные модули "
        "используют одну согласованную позицию и общий журнал вопросов. Ответственных со стороны "
        "заказчика назначают до запуска; одинаковые работы не включают в объём дважды.",
    )

    for i, product in enumerate(products, 1):
        story.append(PageBreak())
        title(3 + i, f"Продукт {i} · {product['code']}", product["name"])
        add(
            "ПРИ ПОДТВЕРЖДЕНИИ ТЕМЫ И ПРИМЕНИМОСТИ"
            if product.get("conditional")
            else "ПРОЕКТ ПИЛОТА · ТРЕБУЕТ СОГЛАСОВАНИЯ",
            size=8,
            color=MUTED,
        )
        section("Границы работы", product["scope"])
        if product.get("rationale"):
            section("Адаптация автора под объект", product["rationale"])
        add(
            "Владелец со стороны заказчика: " + ", ".join(product["buyer_roles"]),
            size=9,
            color=MUTED,
        )
        matching = ", ".join(t.replace("_", " ") for t in product["matching_topics"])
        basis = (
            f"В карточке отмечены темы: {matching}. "
            if matching
            else "Сначала требуется устранить неопределённость в исходных данных. "
        )
        refs = ", ".join(f"[{numbers[u]}]" for u in product["evidence_urls"] if u in numbers)
        section(
            "Почему подходит этому ЖК",
            basis
            + (
                f"Связанные публикации: {refs}. "
                if refs
                else "Основание — тематическая разметка карточки. Применимость требует проверки. "
            )
            + "При старте проверить: "
            + product["starting_question"],
        )
        if product.get("keyword_topics"):
            add(
                "Часть тем выделена по словам исходного описания. Это основание для вопроса, "
                "а не подтверждение проблемы.",
                size=8,
                color=MUTED,
            )
        if product.get("case_trigger"):
            section(
                "Сигнал, который предстоит разобрать",
                product.get("case_origin", "Материал карточки")
                + ": «"
                + product["case_trigger"]
                + "»",
            )
        add("Точечная адаптация: состав работ", size=12, color=NAVY)
        for n, task in enumerate(product["tasks"], 1):
            add(f"{n:02d}. {task}", size=10)
        section("Что получит заказчик", product["result"])
        section(
            "Как измерять результат",
            product["measurement"] + " Целевые значения согласовать "
            "после исходного замера; сейчас они не установлены.",
        )

    selected_cases = {c for product in products for c in product.get("case_ids", [])}
    for case in brief.get("cases", []):
        if case["id"] in selected_cases:
            section("Опыт агентства: " + case["title"], case["description"])
            section("Подтверждённый результат другого проекта", case["result"])
            add(
                "Основание: " + case["evidence"] + ". Это не гарантия результата для данного ЖК.",
                size=8,
                color=MUTED,
            )

    story.append(PageBreak())
    title(4 + len(products), "План запуска", "Пилот на 30 дней")
    add(
        "Это предлагаемый порядок работ, а не обещание устранить строительные проблемы за месяц. "
        "Отсчёт начинается после согласования объёма и предоставления исходных материалов."
    )
    phases = [
        (
            "Дни 1–5",
            "Установить факты",
            (
                "Сверить корпус, даты и источники; утвердить список вопросов и "
                "владельцев. Результат: исходный замер и перечень пробелов."
            ),
        ),
        (
            "Дни 6–10",
            "Согласовать решение",
            (
                "Выбрать профильные модули, подготовить первую версию материалов и "
                "согласовать её с техническим, клиентским и юридическим блоками."
            ),
        ),
        (
            "Дни 11–23",
            "Запустить пилот",
            (
                "Использовать согласованные материалы в выбранных каналах; вести "
                "журнал обращений и решений. Раз в неделю корректировать ответы по "
                "новым фактам."
            ),
        ),
        (
            "Дни 24–30",
            "Оценить изменения",
            (
                "Повторить замер на сопоставимой выборке. Отдельно показать "
                "выполненные действия, изменения показателей и проблемы, которые "
                "остались открытыми."
            ),
        ),
    ]
    rows = [[p("Период", 9, NAVY), p("Контрольная точка", 9, NAVY)]]
    rows += [[p(period, 9), p(label + "\n" + detail, 10)] for period, label, detail in phases]
    table = Table(rows, colWidths=[77, WIDTH - 77], repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), PALE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
                ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor("#dce5e9")),
            ]
        )
    )
    story.extend([table, Spacer(1, 20)])
    section(
        "Что запросить на первой встрече",
        brief.get("check", "") + " Назначить владельца проекта, "
        "получить актуальные документы и перечень открытых каналов. Уточнить уже принятые меры, "
        "порядок согласования ответов и ограничения на публикацию сведений.",
    )
    section(
        "Решение по итогам",
        "Продлить только модули с понятной задачей и измеримым результатом. "
        "Если факты не подтвердились или отсутствует уполномоченный заказчик — "
        "пересмотреть предложение. "
        "Стоимость и трудозатраты определяются после согласования объёма.",
    )

    story.append(PageBreak())
    title(5 + len(products), "Приложение", "Источники и методика")
    section(
        "Команда и основание продуктов",
        "Со стороны Мегаполиса: руководитель проекта "
        "и аналитик; профильные специалисты подключаются по согласованному объёму. "
        "Названия и состав пилотов требуют вычитки агентством.",
    )
    for product in products:
        add(
            product["code"]
            + " · "
            + product["name"]
            + ": презентация Мегаполиса, слайды "
            + ", ".join(map(str, product["source_pages"]))
            + ".",
            size=8,
            color=MUTED,
        )
    add(
        f"Происхождение: {brief.get('file', '')} / "
        f"{brief.get('sheet', '')} / строка {brief.get('row', '')}.",
        size=9,
    )
    add(
        "Все ссылки приведены из сохранённых материалов. Дата публикации не приравнивается "
        "к дате события. Перепечатки не считаются независимым подтверждением. "
        "Краткий поисковый фрагмент имеет меньшую полноту, чем прочитанная страница.",
        size=9,
    )
    for url in urls:
        s = next((s for s in signals if s["source_url"] == url), {})
        link(f"[{numbers[url]}] {s.get('source_title') or urlsplit(url).netloc}", url)
        add(url, size=8, color=MUTED, space=5)
        if s:
            add(
                f"Тип: {s.get('kind_label', 'Публикация')}. "
                f"Дата события: {s.get('event_date') or 'не установлена'}. "
                f"Дата публикации: {s.get('published_at') or 'не установлена'}.",
                size=8,
            )
        quotes = list(
            dict.fromkeys(
                [s["quote"] for s in signals if s["source_url"] == url and s.get("quote")]
                + [e["quote"] for e in brief.get("evidence", []) if e["url"] == url]
            )
        )
        for quote in quotes:
            add("«" + quote + "»", size=9)
    if not urls:
        add(
            "В карточке нет ссылок на первоисточники. Демо-материалы не являются "
            "реальными публикациями."
        )
    if stats and stats.get("examples"):
        add("Примеры из открытых обсуждений", size=13, color=NAVY)
        add(
            "Фрагменты сообщений иллюстрируют разметку тем. Это не доказательство нарушения "
            "и не репрезентативный опрос жителей.",
            size=9,
            color=MUTED,
        )
        for example in stats["examples"]:
            add(
                example["title"]
                + " · "
                + datetime.fromisoformat(example["sent_at"])
                .astimezone(ZoneInfo("Europe/Moscow"))
                .strftime("%d.%m.%Y")
                + " · "
                + example["topic"].replace("_", " "),
                size=9,
                color=NAVY,
            )
            add("«" + example["text"] + "»", size=9)
            if example.get("url"):
                link("Открыть сообщение", example["url"])
    section(
        "Как читать рекомендации",
        "Подбор выполнен по темам и сохранённым сведениям карточки. "
        "Это сценарии работ, не независимая экспертиза и не утверждение о виновности участников. "
        "Перед отправкой клиенту агентство проверяет факты, применимость продуктов и формулировки.",
    )

    def page(canvas, document):
        canvas.saveState()
        # Original supplied logo, cropped with a PDF clipping path; no invented redraw.
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
        canvas.setFont("Radar", 7)
        canvas.setFillColor(MUTED)
        canvas.drawRightString(
            A4[0] - 44,
            A4[1] - 48,
            "УЧЕБНЫЙ ПРИМЕР" if brief.get("dataset") == "demo" else "РАБОЧИЙ ДОКУМЕНТ",
        )
        canvas.setStrokeColor(colors.HexColor("#dce5e9"))
        canvas.line(44, 40, A4[0] - 44, 40)
        stamp = brief.get("created_at")
        stamp = (
            datetime.fromisoformat(stamp).astimezone(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y")
            if stamp
            else "Черновик"
        )
        canvas.drawString(44, 27, f"{stamp} · версия {brief.get('version', 'предпросмотр')}")
        canvas.drawRightString(A4[0] - 44, 27, f"Мегаполис Интеледженс · {document.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=page, onLaterPages=page)
    return stream.getvalue()
