"""Keep named publisher attribution tied to the saved source for a claim."""

import re
from urllib.parse import urlsplit

from radar.reports.fact_numbers import MONTH, date_literals
from radar.reports.factual_copy import (
    _facts,
    normalize_proven_delay_duration,
    validate_copy_numbers,
)
from radar.reports.pdf_evidence import _CAUSE, _company_position, _first_party_action, _grounded

_HOST = r"[\w-]+(?:\.[\w-]+)*\.[^\W\d_][\w-]*"
_PRIMARY = re.compile(rf"\bпо\s+данным\s+«?({_HOST})»?", re.I)
_SOURCE = re.compile(rf"(?<!\w){_HOST}(?!\w)", re.I)
_DEADLINE = re.compile(
    r"\b(?:(?:сдвиг|перенос)\w*[^;.!?]{0,60}?\bключ\w*|"
    r"ключ\w*[^;.!?]{0,35}?\b(?:сдвинут|перенес[её]н|перенесли)\w*)\s+"
    r"(?:с\s+(?P<previous>[^;?!\n]{1,44}?)\s+)?на\s+", re.I
)
_FORUM = re.compile(rf"\bна\s+форуме(?:\s+«?({_HOST})»?)?\b", re.I)
_CLAUSE_BREAK = re.compile(r"(?<=[.!?])\s+|[;\n]+")
_DATED_SHIFT = re.compile(
    r"(?P<explicit_actor>(?:застройщик|девелопер)\s+)?«[^»\n]+»\s+"
    r"(?:объявил[аи]?\s+о\s+переносе|сдвинул[аи]?|перен[её]с(?:ла|ли)?)\s+"
    r"(?:срок\s+)?(?:выдач|передач)\w+\s+ключ\w+|"
    r"\b(?:выдача|передача)\s+ключей\s+(?:сдвинута|перенес[её]на)\b", re.I,
)
_JOINT_MEDIA_SIGN = re.compile(
    rf"(?:публикации|материалы)\s+«?({_HOST})»?\s+и\s+«?({_HOST})»?\s+"
    rf"(?:зафиксировали|описали|осветили)\s+(?:перенос|сдвиг)\s+срока"
    rf"(?:\s+(?:выдачи|передачи)\s+ключей)?\s+и\s+реакцию\s+(?:дольщиков|покупателей)\s+"
    rf"в\s+{MONTH}\s*[-–—]\s*{MONTH}\s+20\d{{2}}(?:\s+г(?:ода)?)?\.?", re.I,
)
_KEY_SHIFT = re.compile(
    r"\b(?:перен[её]с(?:ла|ли)?|сдвинул[аи]?|объявил[аи]?\s+о\s+переносе)\s+"
    r"(?:срок\s+)?(?:выдач|передач)\w+\s+ключ\w*|"
    r"\b(?:выдач|передач)\w+\s+ключ\w*\s+(?:перенес[её]н\w*|перенесли|сдвинут\w*)\b",
    re.I,
)
_SHIFT_REASON = re.compile(r"\b(?:перенос|сдвиг|задерж)\w*\s+(?:срок\w*|выдач\w*)\b", re.I)
_NONFACTUAL_SHIFT = re.compile(
    r"\b(?:не|может|если|планирует)\s+(?:быть\s+)?(?:перен[её]с\w*|сдвину\w*|объявил\w*)\b",
    re.I,
)


def _host(value):
    return value.casefold().removeprefix("www.")


def _publishers(sources):
    return {row["n"]: _host(urlsplit(row.get("url") or "").hostname or "") for row in sources}


def _includes_date(wanted, available):
    year, month, day = wanted
    return any(y == year and m == month and (day is None or d == day) for y, m, d in available)


def attribute_media_stage_sign(text, facts, sources, refs):
    """Separate a shared media claim when the quoted sources prove different roles."""
    match = _JOINT_MEDIA_SIGN.fullmatch(text.strip())
    if not match:
        return text
    named = {_host(match[1]), _host(match[2])}
    hosts = _publishers(sources)
    articles = {
        source["n"]: source for source in sources
        if source["n"] in refs and hosts[source["n"]] in named
        and source.get("contour") == "media"
        and source.get("kind", source.get("source_kind")) != "snippet"
    }
    if len(named) != 2 or {hosts[n] for n in articles} != named:
        return text
    shifts, explanations = set(), set()
    for fact in _facts(facts):
        n = fact["source_n"]
        if n not in articles or fact.get("kind") != "fact" or not _grounded(fact, articles[n]):
            continue
        quote = fact["verbatim"]
        if _KEY_SHIFT.search(quote) and not _NONFACTUAL_SHIFT.search(quote):
            shifts.add(hosts[n])
        if (_SHIFT_REASON.search(fact.get("text") or "") and _CAUSE.search(quote)
                and _first_party_action(fact) and _company_position(fact, articles[n], {})):
            explanations.add(hosts[n])
    if len(shifts) != 1 or len(explanations) != 1 or shifts == explanations:
        return text
    return (f"{shifts.pop()} сообщил о переносе срока выдачи ключей. "
            f"{explanations.pop()} привела объяснение застройщика.")


def attribute_published_handover(text, facts, sources, refs=None, *, event_date=None):
    """Name the outlet when the only exact date belongs to the reported deadline shift."""
    match = _DATED_SHIFT.search(text)
    if not match:
        return text
    prefix = text[:match.start()]
    if prefix.strip():
        prefix_dates = date_literals(prefix)
        if len(prefix_dates) != 1:
            return text
        start, end, _ = prefix_dates[0]
        year_suffix = prefix[end:].strip().casefold()
        if prefix[:start].strip() or year_suffix not in {"", "г", "г.", "года"}:
            return text
    dates = [value for _, _, value in date_literals(event_date or prefix) if value[2]]
    if len(dates) != 1:
        return text
    handover_clause = _CLAUSE_BREAK.split(text[match.end():], maxsplit=1)[0]
    wanted = [value for _, _, value in date_literals(handover_clause) if value[1]]
    if not wanted:
        return text
    day = dates[0]
    hosts = _publishers(sources)
    dated_media = {
        row["n"] for row in sources
        if row.get("contour") == "media" and (refs is None or row["n"] in refs)
        and any(
            value == day for key in ("published_at", "evidence_date", "date")
            for _, _, value in date_literals(str(row.get(key) or ""))
        )
    }
    publishers = set()
    for row in _facts(facts):
        if refs is not None and row["source_n"] not in refs:
            continue
        proof = row["verbatim"]
        if not re.search(r"\b(?:перен[её]с|перенос|сдвиг)\w*\b", proof, re.I):
            continue
        available = [value for _, _, value in date_literals(proof)]
        if not all(_includes_date(value, available) for value in wanted):
            continue
        if day in available:
            return text
        if row["source_n"] in dated_media and hosts[row["source_n"]]:
            publishers.add(hosts[row["source_n"]])
    if len(publishers) != 1:
        return text
    publisher = publishers.pop()
    if match["explicit_actor"]:
        return f"{prefix}{publisher} сообщил, что {text[match.start():]}"
    return prefix + publisher + " сообщил о переносе выдачи ключей" + text[match.end():]


def attribute_secondary_source(text, facts, sources, refs):
    """Name the actual publisher when its cited quote refers to another domain."""
    hosts = _publishers(sources)
    cited = {hosts.get(n) for n in refs}

    def replace(match):
        named = _host(match[1])
        if named in cited:
            return match[0]
        publishers = {
            hosts.get(row["source_n"]) for row in _facts(facts)
            if row["source_n"] in refs and any(
                _host(domain[0]) == named for domain in _SOURCE.finditer(row["verbatim"])
            )
        } - {None, ""}
        if len(publishers) != 1:
            return match[0]
        return f"по {publishers.pop()} со ссылкой на {match[1]}"

    return _PRIMARY.sub(replace, text)


def missing_deadline_publishers(text, facts, sources):
    """Check every named outlet to which a clause attributes a key deadline shift."""
    hosts = _publishers(sources)
    missing = set()
    for clause in _CLAUSE_BREAK.split(text):
        deadline = _DEADLINE.search(clause)
        if not deadline:
            continue
        named = {_host(match[0]) for match in _SOURCE.finditer(clause[:deadline.start()])}
        if not named or not named <= set(hosts.values()):
            continue
        suffix = clause[deadline.end():]
        dates = [value for start, _, value in date_literals(suffix) if start == 0 and value[1]]
        if not dates:
            continue
        wanted = {dates[0], *(
            value for _, _, value in date_literals(deadline["previous"] or "") if value[1]
        )}
        published = {
            hosts.get(row["source_n"]) for row in _facts(facts)
            if all(_includes_date(value, [item for _, _, item in date_literals(row["verbatim"])])
                   for value in wanted)
        }
        missing.update(named - published)
    return sorted(missing)


def forum_numbers_supported(text, facts, sources, refs):
    """A quoted forum's numbers cannot come from a neighboring press citation."""
    hosts = _publishers(sources)
    for clause in _CLAUSE_BREAK.split(text):
        forum = _FORUM.search(clause)
        if not forum:
            continue
        named = _host(forum[1]) if forum[1] else None
        scoped = [
            row["n"] for row in sources if row["n"] in refs and (
                (named and hosts[row["n"]] == named)
                or (not named and row.get("contour") in {"reviews", "communities"})
            )
        ]
        checked = normalize_proven_delay_duration(clause, facts, scoped)
        try:
            validate_copy_numbers(checked, facts, scoped)
        except ValueError:
            return False
    return True
