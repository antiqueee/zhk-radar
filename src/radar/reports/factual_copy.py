"""Numeric/date grounding for V8 author copy using cited atomic Facts only.

This is not a semantic fact checker. The independent critic still checks scope,
attribution, causal claims and proposals. The narrow collective-action guard stops
a known numeric fabrication: borrowing a group size from one fact and an action
from another. No prose length limits or approximate quote repair are applied.
"""

import re
from copy import deepcopy

from radar.reports import schema_v8 as contract
from radar.reports.fact_numbers import (
    CARDINALS,
    MONTH,
    NUMERIC,
    date_literals,
    numeric_values,
    validate_fact_numbers,
)
from radar.reports.research_anchors import fact_registry

VERSION = "atomic-copy-grounding-v1"
_MONTH_YEAR = re.compile(r"(0?[1-9]|1[0-2])\.((?:19|20)\d{2})")
_DAY_MONTH = re.compile(r"(0?[1-9]|[12]\d|3[01])\.(0?[1-9]|1[0-2])\.?$")
_PERCENT = re.compile(r"(" + NUMERIC.pattern + r")\s*(?:%|процент\w*)", re.I)
_COUNT = "(?:" + NUMERIC.pattern + "|" + "|".join(CARDINALS) + ")"
_PEOPLE = re.compile(
    r"(?P<count>" + _COUNT + r")\s+(?:[а-яё-]{2,20}\s+){0,2}?"
    r"(?:дольщик\w*|покупател\w*|жител\w*|собственник\w*|граждан\w*|"
    r"человек\w*|чел\b|людей|жильц\w*|участник\w*|подписант\w*|"
    r"сем(?:ья|ьи|ей|ьям|ьями|ьях))", re.I
)
_PAREN_PEOPLE = re.compile(
    r"(?:дольщик\w*|покупател\w*|жител\w*|собственник\w*|участник\w*)\s*"
    r"\(\s*(?P<count>" + _COUNT + r")\s*(?:чел(?:овек\w*)?)?\s*\)", re.I
)
_COLLECTIVE = re.compile(
    r"обратил\w*|пожаловал\w*|подписал\w*|подали|подал\w*|направил\w*|"
    r"собрал\w*|объединил\w*|участвовал\w*|вышл\w*|митинг\w*|"
    r"протест\w*|петиц\w*|коллективн\w*\s+обращен\w*", re.I
)
_ACTION_ITEMS = re.compile(
    r"(?P<count>" + _COUNT + r")\s+(?:коллективн\w*\s+)?"
    r"(?P<item>обращен\w*|жалоб\w*|петиц\w*|подпис[ьи]\w*)", re.I
)
_KEYS = re.compile(r"\bключ(?:и|ей|ам|ами|ах|ом|а|у)?\b", re.I)
_SCOPE_KIND = (
    r"корпус(?:а|е|у|ом|ы|ов|ам|ами|ах)?|очеред(?:ь|и|ью|ей|ям|ями|ях)"
    r"|этап(?:а|е|у|ом|ы|ов|ам|ами|ах)?"
)
_ORDINAL_SUFFIX = r"-(?:й|я|го|м|му|ю|ая|ый|ой|ым|е)"
_SCOPE_NUMBER = rf"\d{{1,3}}(?:\.\d{{1,3}})?(?:{_ORDINAL_SUFFIX}|-?[а-яёa-z])?"
_SCOPE_ROMAN = dict(zip(("i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"),
                        map(str, range(1, 11)), strict=True))
_SCOPE_ORDINAL = (
    "перв", "втор", "трет", "четверт", "пят", "шест", "седьм", "восьм", "девят", "десят",
)
_SCOPE_SYMBOL = rf"(?:{_SCOPE_NUMBER}|{'|'.join(_SCOPE_ROMAN)})"
_SCOPE_INDEX = (
    rf"(?:{_SCOPE_SYMBOL}|"
    rf"(?:{'|'.join(_SCOPE_ORDINAL)})(?:ый|ий|ой|ого|его|ом|ем|ая|яя|ую|юю|ое|ее|ьей))"
)
_SCOPE_SEPARATOR = r"\s*(?:[–—-]|,|\bи\b)\s*(?:№\s*)?"
_SCOPE_AFTER = re.compile(
    rf"\b(?P<kind>{_SCOPE_KIND})\s*(?P<ids>(?:№\s*)?{_SCOPE_SYMBOL}"
    rf"(?:{_SCOPE_SEPARATOR}{_SCOPE_SYMBOL})*)(?!\w|\.\d)",
    re.I,
)
_SCOPE_BEFORE = re.compile(
    rf"(?<!\w)(?P<ids>(?:№\s*)?{_SCOPE_INDEX}(?:{_SCOPE_SEPARATOR}{_SCOPE_INDEX})*)"
    rf"\s+(?P<kind>{_SCOPE_KIND})\b", re.I,
)
_SCOPE_INDEX_PATTERN = re.compile(rf"{_SCOPE_INDEX}(?!\w)", re.I)
_CLAUSE_BOUNDARY = re.compile(
    r"[.!?](?=\s|$)|[;\n]|,\s*(?=а\b|но\b|при этом\b|тогда как\b)", re.I,
)
_READINESS = re.compile(
    r"\b(?:готовност\w+|готов[аоы]?|выполн\w*|построен\w*|возвед[её]н\w*)\b", re.I,
)
# A comma may separate the project percentage from a scoped construction update.
_READINESS_BOUNDARY = re.compile(
    r",\s*(?=(?:готовность\s+(?:объекта|проекта|ЖК)\b|"
    r"(?:(?:в|на|по)\s+(?:[^,\n]|,(?=\s*\d)){1,60}\s+)?"
    r"(?:монолит\w*|кладк\w*|монтаж\w*|вед[её]тся|ведутся|ид[её]т|идут)\b))",
    re.I,
)
_FOUNDATION_ONLY = re.compile(
    r"\b(?P<negation>не\s+)?(?:только|лишь)\s+(?:один\s+)?фундамент(?:а|е|ом|ы|ов)?\b",
    re.I,
)
_FOUNDATION_DATE_BOUNDARY = re.compile(
    rf",\s*(?=(?:(?:к|в|на|по\s+состоянию\s+на)\s+)?"
    rf"(?:\d{{1,2}}\s+)?{MONTH}(?=\s|[,;:.!?]|$))",
    re.I,
)
_DELAY = re.compile(
    r"\b(?:перен(?:ос|[её]с)\w*|сдви[гнж]\w*|задерж\w*|просроч\w*|отлож\w*)\b", re.I,
)
_DELAY_LINK = re.compile(
    r"[\s:—–-]*(?:(?:срок\w*|сдач\w*|ввод\w*|выдач\w*|передач\w*|ключ\w*|"
    r"на|состав\w*|был[аио]?|уже|ровно|равен|всего)\b[\s:—–-]*)*",
    re.I,
)
_MONTH_UNIT = r"(?:мес\.|месяц(?:а|ев)?)(?!\w)"
_WHOLE_MONTHS = re.compile(rf"(?<![\w.,+\-–—~≈])([1-9]\d{{0,2}})\s*{_MONTH_UNIT}", re.I)
_YEARS_MONTHS = re.compile(
    rf"(?<![\w.,+\-–—~≈])(?P<years>(?P<year_count>[1-9]\d?)\s*(?:г\.|года?|лет))\s+(?:и\s+)?"
    rf"(?P<months>(?P<month_count>1[01]|[1-9])\s*{_MONTH_UNIT})",
    re.I,
)
_DELAY_KIND = {
    "keys": _KEYS,
    "commissioning": re.compile(r"\bввод\w*\b", re.I),
    "delivery": re.compile(r"\bсдач\w*\b", re.I),
}
_NOTE_SENTENCE_BREAK = re.compile(r"(?<=[.!?])(\s+)(?=[А-ЯЁA-Z«\"])", re.M)
_AGENCY_DELIVERABLE_SENTENCE = re.compile(
    r"^\s*Агентство\s+(?:подготовит|готовит)\s+(?:[а-яё-]+\s+){0,2}"
    r"(?:справку|материал\w*|документ\w*|позицию|план\w*|протокол\w*|скрипт\w*)\b",
    re.I,
)
_BUYER_WORD = re.compile(
    r"\b(?:дольщик\w*|покупател\w*|собственник\w*|жильц\w*|жител\w*|форум\w*)\b", re.I
)
_NEAREST_PROJECT_DEADLINE = re.compile(
    r"Ближайший документированный срок\s*[:–—-]\s*(?P<date>.+)", re.I
)
_DEADLINE_DATE_END = re.compile(r"\s*(?:года|г\.)?\s*[.!?]?", re.I)
_PUBLISHED_KEY_DEADLINE = re.compile(
    r"\b(?:срок\w*|план\w*|перен[её]с\w*|готов\w*\s+выдава\w*)\b", re.I
)
_KEY_HANDOVER = re.compile(r"\b(?:выда\w*|переда\w*|получ\w*)\b", re.I)
_DEADLINE_TARGET = re.compile(r"\b(?:на|до|к|в)\s*$", re.I)
_FORMER_DEADLINE = re.compile(r"\b(?:прежн\w*|первоначальн\w*|раньше|ранее)\b", re.I)
_COMMISSIONING_DEADLINE = re.compile(
    r"\b(?:срок\w*\s+сдач\w*|ввод\w*\s+(?:в\s+)?эксплуатаци\w*)\b", re.I
)


def _data(value):
    return value.model_dump() if hasattr(value, "model_dump") else value


def _facts(value):
    value = _data(value)
    return [_data(row) for row in (value.get("facts", []) if isinstance(value, dict) else value)]


def _fact_text(row):
    return "\n".join(str(row.get(k) or "") for k in ("text", "date", "verbatim"))


def _without_dates(text):
    result = list(text)
    for start, end, _ in date_literals(text):
        result[start:end] = " " * (end - start)
    return "".join(result)


def _collective_counts(text):
    """Explicit nearby actor/action pairs; unrelated dates are not group sizes."""
    result = set()
    text = _without_dates(str(text))
    # The dot in '658 чел.)' is an abbreviation, not a sentence boundary.
    text = re.sub(r"\bчел\.(?=\s|\)|,|$)", "человек", text, flags=re.I)
    for sentence in re.split(r"[.!?;\n]", text):
        actors = [*_PEOPLE.finditer(sentence), *_PAREN_PEOPLE.finditer(sentence)]
        for action in _COLLECTIVE.finditer(sentence):
            if not actors:
                continue
            # In '658 doubt, while 700 submitted', only the nearest count can
            # describe the action. A number elsewhere in the atom is insufficient.
            nearest = min(actors, key=lambda m: min(
                abs(m.start() - action.end()), abs(action.start() - m.end())
            ))
            distance = min(abs(nearest.start() - action.end()),
                           abs(action.start() - nearest.end()))
            if distance <= 120:
                result.update((value, "people") for value in numeric_values(nearest["count"]))
        for match in _ACTION_ITEMS.finditer(sentence):
            item = next(stem for stem in ("обращен", "жалоб", "петиц", "подпис")
                        if match["item"].casefold().startswith(stem))
            result.update((value, item) for value in numeric_values(match["count"]))
    return result


def _validate_collective_binding(text, rows, path):
    wanted = _collective_counts(text)
    if not wanted:
        return
    # Only source fragments, not a model's paraphrase, establish the actor/action
    # relationship. Different atoms are deliberately never concatenated here.
    supported = set().union(*(_collective_counts(row.get("verbatim") or "") for row in rows))
    missing = wanted - supported
    if missing:
        raise ValueError(path + ": число и коллективное действие не подтверждены одним "
                         "точным фактом: " + ", ".join(sorted(n for n, _ in missing)))


def _scope_index(value):
    value = value.casefold().replace("ё", "е")
    if value in _SCOPE_ROMAN:
        return _SCOPE_ROMAN[value]
    for index, stem in enumerate(_SCOPE_ORDINAL, 1):
        if value.startswith(stem):
            return str(index)
    value = re.sub(_ORDINAL_SUFFIX + r"$", "", value).replace("-", "")
    return re.sub(r"^0+(?=\d)", "", value)


def _named_scopes(text):
    scopes = set()
    for expression in (_SCOPE_AFTER, _SCOPE_BEFORE):
        for match in expression.finditer(text):
            kind = next(
                kind for kind in ("корпус", "очеред", "этап")
                if match["kind"].casefold().startswith(kind)
            )
            ids = match["ids"]
            tokens = list(_SCOPE_INDEX_PATTERN.finditer(ids))
            values = [_scope_index(token[0]) for token in tokens]
            scopes.update((kind, value) for value in values)
            for left, right, start, end in zip(
                tokens, tokens[1:], values, values[1:], strict=False
            ):
                separator = ids[left.end():right.start()].replace("№", "").strip()
                if start.isdecimal() and end.isdecimal() and separator in {"-", "–", "—"}:
                    scopes.update((kind, str(value)) for value in range(int(start) + 1, int(end)))
    return scopes


def _fact_clauses(text):
    masked = _without_dates(text)
    start = 0
    for sep in _CLAUSE_BOUNDARY.finditer(masked):
        yield text[start:sep.start()], masked[start:sep.start()]
        start = sep.end()
    yield text[start:], masked[start:]


def _handover_claims(text):
    for clause, masked in _fact_clauses(text):
        if _KEYS.search(clause) and (scope := _named_scopes(masked)):
            yield scope, [date for _, _, date in date_literals(clause)]


def _date_supported(wanted, evidence):
    return any(
        all(part is None or part == known for part, known in zip(wanted, date, strict=True))
        for date in evidence
    )


def strip_buyer_note_agency_work(text):
    """Drop an unconditional agency deliverable sentence from a buyer summary."""
    parts = _NOTE_SENTENCE_BREAK.split(text)
    removals, has_buyer_prose = [], False
    angle_depth, straight_quote = 0, False
    for index in range(0, len(parts), 2):
        sentence = parts[index]
        agency = _AGENCY_DELIVERABLE_SENTENCE.match(sentence)
        if agency and not (angle_depth or straight_quote):
            removals.append(index)
        elif not sentence.lstrip().casefold().startswith("агентство") and _BUYER_WORD.search(
            sentence
        ):
            has_buyer_prose = True
        angle_depth = max(0, angle_depth + sentence.count("«") - sentence.count("»"))
        straight_quote ^= bool(sentence.count('"') % 2)
    if not (removals and has_buyer_prose):
        return text
    for index in removals:
        parts[index] = ""
        if index:
            parts[index - 1] = ""
    return "".join(parts).strip()


def _date_edge(date, *, latest=False):
    year, month, day = date
    if month == "q":
        month, day = day * 3 - (0 if latest else 2), None
    return year, month or (12 if latest else 1), day or (31 if latest else 1)


def name_project_deadline(text, facts):
    """Name the key handover if a cited earlier commissioning makes 'nearest' misleading."""
    if not (match := _NEAREST_PROJECT_DEADLINE.fullmatch(text)):
        return text
    deadline = match["date"].strip()
    dates = date_literals(deadline)
    if len(dates) != 1 or dates[0][0] or not _DEADLINE_DATE_END.fullmatch(deadline[dates[0][1]:]):
        return text
    wanted = dates[0][2]
    key_dates, commissioning = [], []
    for row in _facts(facts):
        for clause, _ in _fact_clauses(row.get("verbatim") or ""):
            dated = date_literals(clause)
            if _KEYS.search(clause):
                if _KEY_HANDOVER.search(clause) and _PUBLISHED_KEY_DEADLINE.search(clause):
                    key_dates.extend(
                        date for start, _, date in dated
                        if _DEADLINE_TARGET.search(clause[:start])
                        or len(dated) == 1 and not _FORMER_DEADLINE.search(clause)
                    )
            elif len(dated) == 1 and _COMMISSIONING_DEADLINE.search(clause) and not (
                _FORMER_DEADLINE.search(clause)
            ):
                commissioning.append(dated[0][2])
    if _date_supported(wanted, key_dates) and any(
        _date_edge(earlier, latest=True) < _date_edge(wanted) for earlier in commissioning
    ):
        return "Срок выдачи ключей — " + deadline
    return text


def validate_handover_scope(text, facts, refs=None, *, path="copy", event_date=None):
    """Keep an explicitly dated handover tied to the named scope in one source fragment."""
    claims = list(_handover_claims(str(text or "")))
    if not claims:
        return
    rows = _facts(facts)
    allowed = set(refs) if refs is not None else {row["source_n"] for row in rows}
    dated_event = [date for _, _, date in date_literals(event_date or "")]
    evidence = [
        (scope, dates, [date for _, _, date in date_literals(row.get("date") or "")])
        for row in rows if row["source_n"] in allowed
        for scope, dates in _handover_claims(row.get("verbatim") or "")
    ]
    for scope, dates in claims:
        if not (dates or dated_event):
            continue
        if not any(
            scope <= supported_scope
            and all(_date_supported(date, supported_dates) for date in dates)
            and all(_date_supported(date, [*supported_dates, *event_dates]) for date in dated_event)
            for supported_scope, supported_dates, event_dates in evidence
        ):
            raise ValueError(path + ": дата выдачи ключей и указанный корпус или очередь "
                             "не подтверждены вместе одним точным фактом")


def _readiness_claims(text):
    if not _READINESS.search(text) or not _PERCENT.search(text):
        return
    for _, masked in _fact_clauses(text):
        for clause in _READINESS_BOUNDARY.split(masked):
            if _READINESS.search(clause) and (scope := _named_scopes(clause)):
                for percent in _PERCENT.finditer(clause):
                    yield frozenset(scope), next(iter(numeric_values(percent[1])))


def validate_readiness_scope(text, facts, refs=None, *, path="copy"):
    """Keep a readiness percentage attached to the same specific group of buildings."""
    claims = set(_readiness_claims(str(text or "")))
    if not claims:
        return
    allowed = set(refs) if refs is not None else None
    evidence = {
        claim
        for row in _facts(facts) if allowed is None or row["source_n"] in allowed
        for claim in _readiness_claims(row.get("verbatim") or "")
    }
    # A percent measuring the group is not a measured percent for each member.
    if claims - evidence:
        raise ValueError(path + ": процент готовности и указанный корпус или очередь "
                         "не подтверждены вместе одним точным фактом")


def _foundation_scopes(text):
    for clause, _ in _fact_clauses(text):
        for observation in _FOUNDATION_DATE_BOUNDARY.split(clause):
            foundation_only = any(
                not match["negation"] for match in _FOUNDATION_ONLY.finditer(observation)
            )
            if foundation_only and (scope := _named_scopes(_without_dates(observation))):
                yield frozenset(scope)


def validate_construction_scope(text, facts, refs=None, *, path="copy"):
    """Keep a foundation-only observation tied to its own cited building scope."""
    claims = set(_foundation_scopes(str(text or "")))
    if not claims:
        return
    allowed = set(refs) if refs is not None else None
    evidence = {
        scope
        for row in _facts(facts) if allowed is None or row["source_n"] in allowed
        for scope in _foundation_scopes(row.get("verbatim") or "")
    }
    if claims - evidence:
        raise ValueError(
            path + ": числа корпусов или очередей не подтверждены одним точным фактом "
            "о наблюдении «только фундамент». Убери номера из этого наблюдения, "
            "сохрани его дату и атрибуцию. Опиши поздний стройотчёт отдельно."
        )


def _delay_before(text, end):
    """Recognize an amount attached directly to delay wording."""
    return any(
        _DELAY_LINK.fullmatch(text[match.end():end]) for match in _DELAY.finditer(text, 0, end)
    )


def normalize_proven_delay_duration(text, facts, refs, *, label=None):
    """Reuse a cited literal delay duration instead of an equivalent whole-month count."""
    if label is not None:
        match = _WHOLE_MONTHS.search(text) if _DELAY.search(label) else None
        if match and match[0] != text.strip():
            return text
        context = label
    else:
        candidates = [match for match in _WHOLE_MONTHS.finditer(text)
                      if _delay_before(text, match.start())]
        match = candidates[0] if len(candidates) == 1 else None
        context = text
    if not match or not refs:
        return text

    wanted = int(match[1])
    allowed = set(refs)
    scope = _named_scopes(_without_dates(context))
    kinds = {kind for kind, pattern in _DELAY_KIND.items() if pattern.search(context)}
    for row in _facts(facts):
        if row["source_n"] not in allowed:
            continue
        proof = row.get("verbatim") or ""
        if _named_scopes(_without_dates(proof)) != scope:
            continue
        source_kinds = {kind for kind, pattern in _DELAY_KIND.items() if pattern.search(proof)}
        if kinds and source_kinds and kinds != source_kinds:
            continue
        for proven in _YEARS_MONTHS.finditer(proof):
            if not _delay_before(proof, proven.start()):
                continue
            if wanted != int(proven["year_count"]) * 12 + int(proven["month_count"]):
                continue
            duration = f'{proven["years"]} {proven["months"]}'
            tail = text[match.end():]
            if duration.endswith(".") and tail.startswith("."):
                tail = tail[1:]
            elif (label is None and match[0].endswith(".") and not duration.endswith(".")
                    and (not tail.strip() or re.match(r"\s+[А-ЯЁA-Z«]", tail))):
                duration += "."
            return text[:match.start()] + duration + tail
    return text


def validate_copy_numbers(text, facts, refs=None, *, metric_value=False, path="copy"):
    """Allow presentation equivalence, never a new date precision or foreign number.

    refs=None is for fields without a citation slot (lead/gap). Other fields must
    supply their own refs. Source pages and access/publication metadata are not
    searched: only the already validated scoped AtomicFacts can support copy.
    MM.YYYY is a month presentation solely in a metric value, not arbitrary prose.
    """
    rows = _facts(facts)
    if refs is not None:
        refs = set(refs)
        known = {row["source_n"] for row in rows}
        if refs - known:
            raise ValueError(path + ": ссылка не содержит подтверждённых Facts")
        rows = [row for row in rows if row["source_n"] in refs]
    proof = "\n".join(_fact_text(row) for row in rows)
    checked = str(text or "")
    if metric_value and (month := _MONTH_YEAR.fullmatch(checked.strip())):
        checked = f"{month[2]}-{int(month[1]):02d}"
    problems = validate_fact_numbers(
        {"text": checked, "date": None, "source_n": 1}, {"n": 1, "text": proof}
    )
    if problems:
        raise ValueError(path + ": " + "; ".join(problems))
    # A cited building number cannot be relabelled as a percentage.
    allowed_percentages = {
        number for match in _PERCENT.finditer(proof) for number in numeric_values(match[1])
    }
    for match in _PERCENT.finditer(checked):
        if not numeric_values(match[1]) <= allowed_percentages:
            raise ValueError(path + ": процент не подтверждён в цитируемых Facts")
    _validate_collective_binding(checked, rows, path)
    validate_handover_scope(checked, rows, path=path)
    validate_readiness_scope(checked, rows, path=path)


def _walk(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def repair_context(error, facts, *, previous=None):
    """Explain a failed copy field using its existing refs, never invent a repair.

    previous is the actual last page/report candidate. With it, foreign sources
    are excluded from the hints, so adding another citation is not suggested as a
    way to glue a date, phase or group size to a different claim. This function
    does not relax validation, alter copy, choose a fact, or add a source.
    """
    message = str(error)
    prefix = message.split(":", 1)[0]
    path = prefix if re.fullmatch(r"(?:situation\.)?[a-z_]+(?:\.[a-z_0-9]+)*", prefix) else None
    refs = None
    if previous is not None and path:
        node = _data(previous)
        parts = path.split(".")
        if (
            isinstance(node, dict) and parts[0] in {"situation", "scenario", "offer"}
            and parts[0] not in node
        ):
            parts = parts[1:]
        ancestors = []
        try:
            for part in parts[:-1]:
                if isinstance(node, dict):
                    ancestors.append(node)
                    node = node[part]
                else:
                    node = node[int(part)]
            if isinstance(node, dict):
                ancestors.append(node)
            for parent in reversed(ancestors):
                candidates = parent.get("refs")
                if candidates is None and parts[-1] in {"note", "status"}:
                    candidates = parent.get("note_refs")
                if candidates is None and "modules" in parts:
                    candidates = parent.get("pain_refs")
                if candidates is not None:
                    refs = sorted(set(candidates))
                    break
        except (KeyError, IndexError, ValueError, TypeError):
            pass
    rows = _facts(facts)
    registry = fact_registry(rows) if rows else {}
    scoped = [
        {"fact_key": key, **row["fact"]} for key, row in registry.items()
        if refs is None or row["fact"]["source_n"] in refs
    ]
    return {
        "path": path, "error": message, "cited_refs": refs,
        "supported_facts": scoped,
        "date_options": [
            {"fact_key": row["fact_key"], "source_n": row["source_n"], "date": row["date"]}
            for row in scoped if row.get("date")
        ],
        "instruction": (
            "Исправь ошибочный элемент по его существующим ссылкам. Не добавляй чужую ссылку "
            "только ради недостающего числа: это не связывает срок, корпус или действие. "
            "Убери неподтверждённую конкретизацию либо неподтверждённое событие. "
            "Не вычисляй новые интервалы в месяцах, доли или суммы: используй абсолютные "
            "даты и показатели из подтверждённых фактов с их собственной атрибуцией. "
            "Изменение даты с дня на месяц/квартал не создаёт доказательства отсутствующего года. "
            "date_options принадлежат своим фактам, а не любому соседнему событию. "
            "Число участников и коллективное действие должны быть вместе в одном verbatim. "
            "Сохрани остальные проверенные элементы страницы."
        ),
    }


def _display_date(value, date_iso, path):
    """DD.MM is allowed only as a display of its separately proven ISO day."""
    raw = str(value or "").strip()
    target = date_literals(date_iso)
    if len(target) != 1 or target[0][:2] != (0, len(date_iso)):
        raise ValueError(path + ": некорректный date_iso")
    iso_value = target[0][2]
    if match := _DAY_MONTH.fullmatch(raw):
        wanted = (int(match[2]), int(match[1]))
        if iso_value[1:] != wanted:
            raise ValueError(path + ": подпись даты не совпадает с date_iso")
        return date_iso
    displayed = date_literals(raw)
    if not displayed:
        # A short '5 февраля' is a display of the proved event, not a separate
        # unscoped source of month/day tokens. No year is added to returned copy.
        full = raw + " " + str(iso_value[0])
        inferred = date_literals(full)
        if len(inferred) == 1 and inferred[0][:2] == (0, len(full)):
            displayed, raw = inferred, full
    if len(displayed) == 1:
        y, m, d = displayed[0][2]
        iy, im, iday = iso_value
        if y != iy or (m is not None and im is not None and m != im) or (
            d is not None and iday is not None and d != iday
        ):
            raise ValueError(path + ": подпись даты не совпадает с date_iso")
    return raw


def validate_situation(situation, facts, selected):
    """Validate and return a V8 Situation; source copy is never rewritten or clipped."""
    value = contract.Situation.model_validate(_data(situation)).model_dump()
    by_n = {_data(s)["n"]: _data(s) for s in selected}
    rows = _facts(facts)
    for node in _walk(value):
        for key, refs in node.items():
            if key.endswith("refs") and not set(refs) <= by_n.keys():
                raise ValueError("situation: ссылка на неизвестный PDF-источник")
    # Fields without explicit refs still use only the selected projection's Facts.
    all_facts = [row for row in rows if row["source_n"] in by_n]
    validate_copy_numbers(value["lead"], all_facts, path="situation.lead")
    validate_copy_numbers(value["gap"], all_facts, path="situation.gap")
    for index, item in enumerate(value["facts"]):
        path = f"situation.facts.{index}"
        if not item["refs"]:
            raise ValueError(path + ": показатель требует ссылки")
        item["value"] = normalize_proven_delay_duration(
            item["value"], all_facts, item["refs"], label=item["label"],
        )
        validate_copy_numbers(item["value"], all_facts, item["refs"],
                              metric_value=True, path=path + ".value")
        validate_copy_numbers(item["label"], all_facts, item["refs"], path=path + ".label")
        month = _MONTH_YEAR.fullmatch(item["value"].strip())
        date_value = f"{month[2]}-{int(month[1]):02d}" if month else item["value"]
        if date_literals(date_value):
            validate_handover_scope(f'{item["label"]} {date_value}', all_facts, item["refs"],
                                    path=path + ".label")
        if _PERCENT.search(item["value"]):
            validate_readiness_scope(f'{item["label"]} {item["value"]}', all_facts, item["refs"],
                                     path=path + ".label")
    for index, event in enumerate(value["timeline"]):
        path = f"situation.timeline.{index}"
        if not event["refs"]:
            raise ValueError(path + ": событие требует ссылки")
        validate_copy_numbers(event["date_iso"], all_facts, event["refs"], path=path + ".date_iso")
        validate_copy_numbers(_display_date(event["date"], event["date_iso"], path + ".date"),
                              all_facts, event["refs"], path=path + ".date")
        validate_copy_numbers(event["text"], all_facts, event["refs"], path=path + ".text")
        validate_handover_scope(event["text"], all_facts, event["refs"], path=path + ".text",
                                event_date=event["date_iso"])
    buyers = value["buyers"]
    for index, quote in enumerate(buyers["quotes"]):
        path = f"situation.buyers.quotes.{index}"
        if not quote["refs"] or not quote["text"] or not any(
            quote["text"] in by_n[n].get("text", "") and any(
                row["source_n"] == n and quote["text"] in row.get("verbatim", "")
                for row in all_facts
            ) for n in quote["refs"]
        ):
            raise ValueError(path + ": цитата должна дословно присутствовать в своём Facts "
                             "и прочитанном источнике")
        validate_copy_numbers(quote["text"], all_facts, quote["refs"], path=path + ".text")
        validate_copy_numbers(quote["attribution"], all_facts, quote["refs"],
                              path=path + ".attribution")
    note_refs = buyers.get("note_refs") or sorted({
        n for item in [*buyers["quotes"], *buyers["topics"]] for n in item["refs"]
    })
    validate_copy_numbers(buyers["note"], all_facts, note_refs, path="situation.buyers.note")
    validate_copy_numbers(buyers["status"], all_facts, note_refs, path="situation.buyers.status")
    for index, topic in enumerate(buyers["topics"]):
        validate_copy_numbers(topic["text"], all_facts, topic["refs"],
                              path=f"situation.buyers.topics.{index}.text")
    for index, point in enumerate(value["company"]["points"]):
        path = f"situation.company.points.{index}"
        public_gap = "в открытых источниках" in point["text"].casefold() and any(
            word in point["text"].casefold() for word in ("нет", "не опубликован", "не найден")
        )
        if not point["refs"] and not public_gap:
            raise ValueError(path + ": позиция компании требует ссылки")
        validate_copy_numbers(point["text"], all_facts, point["refs"], path=path + ".text")
    def chronological(event):
        year, month, day = date_literals(event["date_iso"])[0][2]
        return year, 3 * day - 2 if month == "q" else month or 1, 1 if month == "q" else day or 1

    value["timeline"].sort(key=chronological)
    return contract.Situation.model_validate(deepcopy(value))
