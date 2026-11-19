"""Compile the local residential catalog into one or two Brand Analytics topics."""

import json
import re
import unicodedata
from collections import defaultdict
from itertools import combinations

from radar.web import stoplist
from radar.web.record_identity import _developers, _known, _name
from radar.web.report_search import _scope_anchor, aliases_for

_HOUSING_PREFIX = re.compile(r"^(?:жк|жилой комплекс)\s+", re.I)
_SCOPE_SUFFIX = re.compile(
    r"[,\s—–-]+(?:"
    r"(?:корпус(?:а|ов)?|корп\.?|дом|литер[аы]?|очередь|этап)\s+[\w./–-]+"
    r"|(?:\d+|[ivx]+)(?:-я)?\s+очередь|отдельные корпуса|\d+\s+корпусов"
    r")\s*$",
    re.I,
)
_NOISY_NAMES = {
    "баланс", "домашний", "новая жизнь", "новая звезда", "парк победы",
    "первое место", "центральный парк", "светлый мир", "северный берег",
}
_GENERIC_NAME = re.compile(
    r"^(?:жк|жилой комплекс|новостройка|жилой дом|жилые кварталы|дом|объект)"
    r"(?:\s+(?:в|на)\s+.+|\s+\d+)?$|^портфель\b",
    re.I,
)
_UNKNOWN_REGION = {"рф", "вся россия", "московский регион", "москва и область"}


def _query_words(value):
    # Removing punctuation prevents catalog text from introducing query operators.
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(re.findall(r"[^\W_]+", normalized, flags=re.UNICODE))


def _phrase(value):
    return f'"{value}"~0'


def _catalog(store):
    with store.connect() as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        exclusions = db.execute("SELECT * FROM stoplist").fetchall() if "stoplist" in tables else []
        research = {}
        if "report_research" in tables:
            research = {
                ident: {"passport": json.loads(passport)}
                for ident, passport in db.execute(
                    "SELECT record_id,json_extract(payload,'$.passport') FROM report_research"
                )
                if passport
            }
        cards = []
        for ident, raw in db.execute("SELECT id,payload FROM records WHERE dataset<>'demo'"):
            card = {**json.loads(raw), "id": ident}
            if card.get("merged_into") or card.get("granularity") == "developer":
                continue
            if stoplist.match({**card, "developer": card.get("developer") or ""}, exclusions):
                continue
            cards.append(card)
    return sorted(cards, key=lambda card: (_name(card.get("name")), card["id"])), research


def _aliases(card, research):
    if not card.get("name"):
        return {}
    result = {}
    for raw in aliases_for(card, research):
        if _GENERIC_NAME.search(_query_words(raw)):
            continue
        base = _HOUSING_PREFIX.sub("", raw)
        candidates = (_SCOPE_SUFFIX.sub("", base), base)
        for candidate in candidates:
            value = _query_words(candidate)
            key = _name(value)
            if len(key) >= 3 and not _GENERIC_NAME.search(value):
                result.setdefault(key, value)
    return result


def _region(card):
    region = _name(card.get("region"))
    return region if _known(region) and region not in _UNKNOWN_REGION else ""


def _context(card, name, key):
    developers = [item for item in _developers(card) if item != key]
    candidates = [
        _query_words(_scope_anchor(card, name)),
        *sorted(developers, key=lambda value: (len(value), value)),
        _region(card),
    ]
    return next((value for value in candidates if len(value) >= 3 and _name(value) != key), "")


def _expressions(card, aliases, collisions):
    result = []
    for key, name in aliases.items():
        context = _context(card, name, key)
        # Repeated names need an object-specific term. The matcher handles finer phase scope.
        if key in collisions:
            if context:
                result.append(f"{_phrase(name)} {_phrase(context)}")
        elif key in _NOISY_NAMES or len(key.split()) == 1:
            if context:
                result.append(f"{_phrase(name)} {_phrase(context)}")
            result.append(_phrase("ЖК " + name))
        else:
            result.append(_phrase(name))
    return list(dict.fromkeys(result))


def _different_owners(contexts):
    return any(
        (region_a and region_b and region_a != region_b)
        or (devs_a and devs_b and not devs_a & devs_b)
        for (region_a, devs_a), (region_b, devs_b) in combinations(contexts, 2)
    )


def _pack(groups, links, max_chars, max_topics):
    topics = []
    lengths = []
    omitted = 0
    for expression in groups:
        for topic, length in enumerate(lengths):
            if max_chars is None or length + 2 + len(expression) <= max_chars:
                topics[topic].append(expression)
                lengths[topic] += 2 + len(expression)
                break
        else:
            if len(topics) >= max_topics or max_chars is not None and len(expression) > max_chars:
                omitted += 1
                continue
            topics.append([expression])
            lengths.append(len(expression))
    return [
        {
            "query": ", ".join(topic),
            "characters": length,
            "card_ids": sorted(set().union(*(links[expression] for expression in topic))),
        }
        for topic, length in zip(topics, lengths, strict=True)
    ], omitted


def build_queries(store, *, max_chars=None, max_topics=2):
    """Produce operator-ready queries without creating vendor topics or mutating the catalog."""
    if (max_chars is not None and max_chars < 1) or max_topics not in (1, 2):
        raise ValueError("Укажите положительный предел длины и одну или две темы")
    cards, research = _catalog(store)
    aliases = {card["id"]: _aliases(card, research.get(card["id"])) for card in cards}
    owners = defaultdict(set)
    for card in cards:
        for key in aliases[card["id"]]:
            owners[key].add((_region(card), frozenset(_developers(card))))
    collisions = {key for key, contexts in owners.items() if _different_owners(contexts)}
    options = {}
    links = defaultdict(set)
    spellings = {}
    for card in cards:
        ident = card["id"]
        options[ident] = [
            spellings.setdefault(expression.casefold().replace("ё", "е"), expression)
            for expression in _expressions(card, aliases[ident], collisions)
        ]
        for expression in options[ident]:
            links[expression].add(ident)
    # Give each card its first expression before packing further spellings.
    primary = [terms[0] for terms in options.values() if terms]
    extra = [term for terms in options.values() for term in terms[1:]]
    queries, omitted = _pack(dict.fromkeys(primary + extra), links, max_chars, max_topics)
    covered = {ident for topic in queries for ident in topic["card_ids"]}
    uncovered = []
    for card in cards:
        ident = card["id"]
        if ident not in covered:
            reason = (
                "query_capacity" if options[ident] else
                "ambiguous_without_context" if aliases[ident] else
                "no_project_name"
            )
            uncovered.append({"id": ident, "name": card.get("name", ""), "reason": reason})
    return {
        "queries": queries,
        "total_cards": len(cards),
        "covered_cards": len(covered),
        "uncovered": uncovered,
        "omitted_expressions": omitted,
    }
