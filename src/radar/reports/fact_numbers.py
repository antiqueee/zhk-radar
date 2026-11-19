"""V5 numeric/date equivalence within one cited source; no arithmetic inference."""

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

MONTHS = (
    r"январ[ьяею]|янв\.",
    r"феврал[ьяею]|февр?\.",
    r"март(?:а|е|у)?|мар\.",
    r"апрел[ьяею]|апр\.",
    r"ма[йяею]",
    r"июн[ьяею]|июн\.",
    r"июл[ьяею]|июл\.",
    r"август(?:а|е|у)?|авг\.",
    r"сентябр[ьяею]|сент?\.",
    r"октябр[ьяею]|окт\.",
    r"ноябр[ьяею]|нояб?\.",
    r"декабр[ьяею]|дек\.",
)
MONTH = "(?:" + "|".join(MONTHS) + ")"
SPACE = r"[ \u00a0\u202f]"
NUMERIC = re.compile(r"(?<!\d)(?:\d{1,3}(?:" + SPACE + r"\d{3})+|\d+)(?:[.,]\d+)?")
CARDINALS = (
    "ноль|нуль",
    "один|одна|одно|одного|одной",
    "два|две|двух",
    "три|трех",
    "четыре|четырех",
    "пять|пяти",
    "шесть|шести",
    "семь|семи",
    "восемь|восьми",
    "девять|девяти",
    "десять|десяти",
    "одиннадцать|одиннадцати",
    "двенадцать|двенадцати",
    "тринадцать|тринадцати",
    "четырнадцать|четырнадцати",
    "пятнадцать|пятнадцати",
    "шестнадцать|шестнадцати",
    "семнадцать|семнадцати",
    "восемнадцать|восемнадцати",
    "девятнадцать|девятнадцати",
)
ORDINALS = ("перв", "втор", "трет", "четверт", "пят", "шест", "седьм", "восьм", "девят", "десят")
ISO_DATETIME = (
    r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?"
    r"(?:[Zz]|[+-]\d{2}:\d{2})?"
)


def _fold(value):
    return str(value or "").casefold().replace("ё", "е")


def _number(value):
    value = re.sub(SPACE, "", value).replace(",", ".")
    whole, _, fraction = value.partition(".")
    whole = whole.lstrip("0") or "0"
    fraction = fraction.rstrip("0")
    return whole + ("." + fraction if fraction else "")


def numeric_values(text):
    """Presentation equivalence only: 1 131 = 1131, 03 = 3, 1,20 = 1.2."""
    text = _fold(text)
    result = {_number(match.group()) for match in NUMERIC.finditer(text)}
    for n, forms in enumerate(CARDINALS):
        if re.search(r"\b(?:" + forms + r")\b", text):
            result.add(str(n))
    for n, stem in enumerate(ORDINALS, 1):
        if re.search(r"\b" + stem + r"(?:ый|ий|ой|ого|его|ом|ем|ая|яя|ую|юю|ое|ее)\b", text):
            result.add(str(n))
    # A building/phase label is the same ordinal in Roman and Arabic notation.
    # Restrict this to explicit Russian labels, never arbitrary Latin letters.
    roman = {
        "i": 1,
        "ii": 2,
        "iii": 3,
        "iv": 4,
        "v": 5,
        "vi": 6,
        "vii": 7,
        "viii": 8,
        "ix": 9,
        "x": 10,
    }
    for match in re.finditer(
        r"(?<!\w)(viii|vii|iii|vi|iv|ii|ix|x|v|i)\s+(?:очеред\w*|корпус\w*|квартал\w*)(?!\w)",
        text,
    ):
        result.add(str(roman[match[1]]))
    return result


def _month(value):
    return next(i for i, pattern in enumerate(MONTHS, 1) if re.fullmatch(pattern, value))


def _valid(y, m=None, d=None):
    try:
        if m is not None:
            date(y, m, d or 1)
        return (y, m, d)
    except ValueError:
        return None


def date_literals(text):
    """Return spans and explicit precision. Never combine unrelated numeric tokens."""
    text = _fold(text)
    result = []

    def add(pattern, convert):
        for match in re.finditer(pattern, text):
            start, end = match.span()
            if any(start < old_end and end > old_start for old_start, old_end, _ in result):
                continue
            value = convert(match)
            if value:
                result.append((start, end, value))

    add(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)", lambda m: _valid(*map(int, m.groups())))
    add(
        r"(?<!\d)(\d{1,2})\.(\d{1,2})\.(\d{4})(?!\d)",
        lambda m: _valid(int(m[3]), int(m[2]), int(m[1])),
    )
    add(
        r"(?<!\w)(\d{1,2})\s*(" + MONTH + r")\s+(\d{4})(?!\d)",
        lambda m: _valid(int(m[3]), _month(m[2]), int(m[1])),
    )
    add(r"(?<!\w)(" + MONTH + r")\s+(\d{4})(?!\d)", lambda m: _valid(int(m[2]), _month(m[1])))
    add(r"(?<!\d)(\d{4})-(\d{2})(?![-\d])", lambda m: _valid(int(m[1]), int(m[2])))
    add(r"(?<!\w)(\d{4})-q([1-4])(?!\w)", lambda m: (int(m[1]), "q", int(m[2])))
    add(
        r"(?<!\w)([1-4]|iv|iii|ii|i)\s*(?:квартал\w*|кв\.)\s*(\d{4})(?!\d)",
        lambda m: (
            int(m[2]),
            "q",
            {"i": 1, "ii": 2, "iii": 3, "iv": 4}.get(m[1], int(m[1]) if m[1].isdigit() else 0),
        ),
    )
    add(r"(?<!\d)(?:19|20)\d{2}(?!\d)", lambda m: (int(m[0]), None, None))
    return sorted(result)


def _publication_dates(source):
    result = []
    for key in ("published_at", "published_date", "publication_date", "date", "evidence_date"):
        value = source.get(key)
        if not value or any(word in _fold(value) for word in ("проверен", "checked", "accessed")):
            continue
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if moment.tzinfo:
                moment = moment.astimezone(ZoneInfo("Europe/Moscow"))
            result.append((moment.year, moment.month, moment.day))
        except ValueError:
            result.extend(value for _, _, value in date_literals(value))
    return result


def _moment(value):
    if not isinstance(value, str) or not re.fullmatch(ISO_DATETIME, value.strip()):
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("z", "+00:00").replace("Z", "+00:00"))
    except ValueError:
        return None


def canonical_fact_date(value, source):
    """A proven ISO timestamp becomes its day; never invent its time or timezone."""
    moment = _moment(value)
    if moment is None:
        return value
    candidates = [
        source.get(key)
        for key in ("published_at", "published_date", "publication_date", "date", "evidence_date")
    ]
    candidates.extend(
        match[0]
        for match in re.finditer(
            r"(?<!\w)" + ISO_DATETIME + r"(?![\w:+-])", source.get("text") or ""
        )
    )
    for candidate in candidates:
        proof = _moment(candidate)
        # A naive wall time cannot prove an aware instant (or vice versa).
        if proof is None or bool(proof.tzinfo) != bool(moment.tzinfo) or moment != proof:
            continue
        if moment.tzinfo:
            moment = moment.astimezone(ZoneInfo("Europe/Moscow"))
        return moment.date().isoformat()
    return value


def _supports(wanted, available, partial_days, publication_years):
    y, m, d = wanted
    if m is None:
        return any(value[0] == y for value in available)
    if m == "q":
        return wanted in available
    if d is None:
        return any(value[0] == y and value[1] == m for value in available)
    return wanted in available or ((m, d) in partial_days and y in publication_years)


def validate_fact_numbers(fact, source):
    """Return precise issues for one fact and its own source; never aggregate sources."""
    if hasattr(fact, "model_dump"):
        fact = fact.model_dump()
    issues = []
    if source.get("n") is not None and fact.get("source_n") != source["n"]:
        return ["source_n факта не совпадает с проверяемым источником"]
    text = source.get("text") or ""
    metadata = _publication_dates(source)
    available = [value for _, _, value in date_literals(text)] + metadata
    original_date = fact.get("date")
    canonical_date = canonical_fact_date(original_date, source)
    if canonical_date != original_date:
        day = date.fromisoformat(canonical_date)
        available.append((day.year, day.month, day.day))
    partial_days = {
        (_month(m[2]), int(m[1]))
        for m in re.finditer(r"(?<!\w)(\d{1,2})\s*(" + MONTH + r")(?!\w)", _fold(text))
        if _valid(2000, _month(m[2]), int(m[1]))
    }
    publication_years = {value[0] for value in metadata}
    allowed_numbers = numeric_values(text)
    for y, m, d in metadata:
        allowed_numbers.add(str(y))
        if isinstance(m, int):
            allowed_numbers.add(str(m))
        if d:
            allowed_numbers.add(str(d))
    fact_text = fact.get("text") or ""
    literals = date_literals(fact_text)
    for start, end, wanted in literals:
        if not _supports(wanted, available, partial_days, publication_years):
            issues.append(
                "Дата в text не подтверждена с исходной точностью: " + fact_text[start:end]
            )
    # Dates have their own precision check. Do not reinterpret their month/day as quantities.
    quantity_text = list(fact_text)
    for start, end, _ in literals:
        quantity_text[start:end] = " " * (end - start)
    for value in sorted(numeric_values("".join(quantity_text)) - allowed_numbers):
        issues.append("Число в text отсутствует в своём источнике: " + value)
    value = canonical_date
    if value:
        raw = _fold(value).strip()
        # These suffixes add no date precision. The whole remaining expression
        # still has to be one supported literal from this same source.
        raw = re.sub(r"(?<=\d{4})\s*(?:года?|г\.)$", "", raw).strip()
        year_range = re.fullmatch(r"((?:19|20)\d{2})\s*[–—-]\s*((?:19|20)\d{2})", raw)
        if year_range:
            if not all(
                _supports((int(y), None, None), available, partial_days, publication_years)
                for y in year_range.groups()
            ):
                issues.append("Диапазон date содержит год вне своего источника: " + str(value))
        else:
            parsed = date_literals(raw)
            if len(parsed) != 1 or parsed[0][0] != 0 or parsed[0][1] != len(raw):
                issues.append("Неподдерживаемая или некорректная дата date: " + str(value))
            elif not _supports(parsed[0][2], available, partial_days, publication_years):
                issues.append(
                    "Дата date отсутствует в своём источнике с такой точностью: " + str(value)
                )
    return issues
