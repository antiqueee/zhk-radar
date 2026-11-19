"""Evidence-preserving PDF projection and immutable buyer quotations for V8.

The full research registry is never edited. Six printed citations are an editorial
projection, not a limit on research facts. Inputs must already have passed the
ordinary fact/qualification gates; this module neither assigns a passport nor
turns a model's attribution into a new fact.
"""

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Annotated, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field, create_model

from radar.reports import schema_v8 as contract
from radar.reports.research_anchors import fact_registry
from radar.web.report_evidence import date_span
from radar.web.sources import is_access_page

SELECTION_VERSION = "evidence-projection-v1"
QUOTE_VERSION = "exact-buyer-choices-v1"
_NON_CHAT = {"official", "authorities", "media", "reviews"}
_COMPANY_ROLE = re.compile(
    r"застройщик|компани|девелопер|президент|директор|представител|пресс.служб", re.I
)
_SPEECH = re.compile(
    r"сообщ|объясн|заяв|указал|отметил|рассказ|подчерк|коммент|по\s+словам", re.I
)
_ACTION = re.compile(
    r"столкнул|пров[её]л|провели|выполн|строим|строит|исслед|измен|перен[её]с|решил|решили",
    re.I,
)
_CAUSE = re.compile(r"причин|из.за|вследств|геолог|грунт|объясн|привел|прив[её]л", re.I)
_CARD_FACT = re.compile(r"срок|сдан|ввод|передач|декларац|разрешени", re.I)
_FIRST_PERSON = re.compile(r"(?<!\w)(?:я|мы|мне|нам|мой|мо[её]м|моя|мои|наш[а-я]*)\b", re.I)
_REPORTING = re.compile(r"по\s+(?:их|его|е[её])\s+словам|они\s+(?:утверждают|сообщают)", re.I)
_ISSUE = re.compile(
    r"срок|перенос|задерж|договор|ключ|передач|ответ|обращен|дефект|протеч|обмен|"
    r"жалоб|претензи|устран|при[её]м|ипотек|неустойк|недостат|ремонт|гаранти", re.I
)


def _data(value):
    return value.model_dump() if hasattr(value, "model_dump") else value


def _facts(value):
    value = _data(value)
    return [_data(row) for row in (value.get("facts", []) if isinstance(value, dict) else value)]


def _key(value):
    return " ".join(re.findall(r"\w+", str(value).casefold().replace("ё", "е")))


def _date(value):
    span = date_span(value)
    return span[0].isoformat() if span else ""


def _url(value):
    parsed = urlsplit(value or "")
    query = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.casefold().startswith("utm_")
        and k.casefold() not in {"gclid", "fbclid", "yclid", "ysclid"}
    ]
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(),
                      parsed.path.rstrip("/"), urlencode(sorted(query)), ""))


def _readable(source):
    text = source.get("text") or ""
    return bool(text.strip()) and not (
        source.get("kind") == "snippet" or source.get("source_kind") == "snippet"
        or is_access_page(source.get("title", ""), text)
    )


def _grounded(fact, source):
    quote = fact.get("verbatim") or ""
    return bool(quote.strip()) and quote in source.get("text", "")


def _about_object(fact, source, passport):
    names = [passport.get("official_name"), *(passport.get("aliases") or [])]
    names = [_key(name) for name in names if name and len(_key(name)) >= 4]
    if not names:
        return True
    # A portfolio-wide interview can contain many projects. The speaker's job
    # title cannot make every paragraph an explanation about the selected object.
    quote = _key(fact.get("verbatim") or "")
    title = _key(source.get("title") or "")
    return any(name in quote or name in title for name in names)


def _first_party_action(fact):
    quote = fact.get("verbatim") or ""
    return bool(re.search(r"(?<!\w)(?:мы|наш[а-я]*)\b", quote, re.I) and _ACTION.search(quote))


def _company_position(fact, source, passport):
    """Require speech/action in the exact fragment, not who_says alone."""
    if source.get("contour") not in {"official", "media", "authorities"}:
        return False
    quote = fact.get("verbatim") or ""
    who = fact.get("who_says") or ""
    names = [passport.get(k) for k in ("developer_brand", "developer_legal", "tech_customer")]
    names = [_key(name) for name in names if name and len(_key(name)) >= 3]
    named_identity = any(name in _key(who) for name in names)
    quoted_identity = any(name in _key(quote) for name in names) and _SPEECH.search(quote)
    generic_official = source.get("contour") == "official" and _COMPANY_ROLE.search(who)
    identity = named_identity or quoted_identity or generic_official or (
        not names and _COMPANY_ROLE.search(who)
    )
    named_speech = (
        (_COMPANY_ROLE.search(quote) or any(name in _key(quote) for name in names))
        and _SPEECH.search(quote)
    )
    return bool(identity and (named_speech or _first_party_action(fact))
                and _about_object(fact, source, passport) and _grounded(fact, source))


def evidence_summary(sources, facts, qualification):
    """Copyable fact keys for positions, causes and chronology, never authored claims."""
    by_n = {s["n"]: s for s in sources}
    passport = qualification.get("passport") or {}
    result = {"company_position": [], "first_party_company_position": [],
              "causes": [], "chronology": []}
    for key, row in fact_registry(facts).items():
        fact = row["fact"]
        source = by_n.get(fact["source_n"])
        if not source or not _readable(source) or not _grounded(fact, source):
            continue
        item = {"fact_key": key, "source_n": fact["source_n"]}
        if _company_position(fact, source, passport):
            result["company_position"].append(item)
            if _first_party_action(fact):
                result["first_party_company_position"].append(item)
        if _CAUSE.search(fact.get("verbatim") or "") and _about_object(fact, source, passport):
            result["causes"].append(item)
        dated = _date(fact.get("date")) or _date(source.get("evidence_date"))
        if dated:
            result["chronology"].append({**item, "date": dated})
    return result


def _redundant(a, b, facts_by_n):
    """One document/contained same-site card does not earn two citation slots."""
    if a.get("url") and _url(a["url"]) == _url(b.get("url")):
        return True
    pa, pb = urlsplit(a.get("url") or ""), urlsplit(b.get("url") or "")
    if not pa.hostname or pa.hostname != pb.hostname or a.get("contour") != b.get("contour"):
        return False
    paths = [p.path.rstrip("/") for p in (pa, pb)]
    if not all(paths) or not (
        paths[0].startswith(paths[1] + "/") or paths[1].startswith(paths[0] + "/")
    ):
        return False
    da, db = _date(a.get("evidence_date")), _date(b.get("evidence_date"))
    if da and db and da != db:
        return False
    # Preserve every unique substantive fragment, even on a page with shared navigation.
    for shorter, other in ((a, b), (b, a)):
        informative = [
            f for f in facts_by_n[shorter["n"]]
            if len((f.get("verbatim") or "").split()) >= 5 or f.get("date")
        ]
        if any(f["verbatim"] not in other.get("text", "") for f in informative):
            continue
        words = re.findall(r"\w+", shorter.get("text", "").casefold())
        other_words = re.findall(r"\w+", other.get("text", "").casefold())
        shingles = {tuple(words[i:i + 5]) for i in range(len(words) - 4)}
        other_shingles = {tuple(other_words[i:i + 5]) for i in range(len(other_words) - 4)}
        if len(shingles) >= 20 and len(shingles & other_shingles) / len(shingles) >= 0.65:
            return True
    return False


class EvidenceSelectionError(ValueError):
    """Required evidence cannot fit the print projection; nothing is silently dropped."""


@dataclass
class Selection:
    sources: list[dict]
    facts: list[dict]
    mapping: dict[int, int]
    coverage: dict


def select_pdf_evidence(sources, facts, qualification, *, limit=6, primary_problem_id=None):
    """Keep mandatory proof and complementary primary evidence within six citations.

    Only source numbers are projected. The returned facts are dictionaries suitable
    for the caller's ResearchFacts contract. Original sources/facts/qualification
    remain byte-for-byte unchanged. Duplicate pages may both survive only when
    indispensable to different mandatory proof groups.
    """
    if not 1 <= limit <= 6:
        raise ValueError("В PDF допустимо от одного до шести источников")
    qualification = _data(qualification)
    rows = _facts(facts)
    sources = [deepcopy(_data(s)) for s in sources]
    by_n = {s["n"]: s for s in sources if _readable(s)}
    facts_by_n = {
        n: [f for f in rows if f["source_n"] == n and _grounded(f, s)]
        for n, s in by_n.items()
    }
    by_n = {n: s for n, s in by_n.items() if facts_by_n[n]}
    if not by_n:
        raise EvidenceSelectionError("Нет прочитанных источников с точными фактами")
    passport = qualification.get("passport") or {}
    required = {}

    def require(name, candidates):
        numbers = set(candidates) & by_n.keys()
        if not numbers:
            raise EvidenceSelectionError("Нет источника обязательного доказательства: " + name)
        required[name] = numbers

    for prefix in ("status", "addressee", "developer_status"):
        refs = set(passport.get(prefix + "_refs") or [])
        indexes = passport.get(prefix + "_fact_indexes") or []
        if indexes:
            refs &= {rows[i]["source_n"] for i in indexes if 0 <= i < len(rows)}
        if passport.get(prefix) not in {None, "none", "unknown"}:
            require(prefix, refs)
    # A validated active addressee also carries the company's identity. Do not
    # reserve a second, obsolete launch story merely for the same brand name.
    identity = set(passport.get("developer_refs") or []) | set(
        passport.get("addressee_refs") or []
    )
    if identity:
        require("identity", identity)
    signs = qualification.get("stage_signs") or []
    current = qualification.get("current_stage")
    stage_sources = {s["source_n"] for s in signs if s.get("stage") == current}
    if stage_sources:
        require("current_stage", stage_sources)
    problems = [
        p for p in qualification.get("problem_registry", [])
        if p.get("in_scope") and p.get("status") == "active"
    ]
    primary = next((p for p in problems if p["id"] == primary_problem_id), None)
    primary = primary or next(iter(problems), {})
    primary_refs = set(primary.get("source_numbers") or []) & by_n.keys()
    evidence = (qualification.get("research_evidence") or {}).get("evidence", [])
    incidents = [
        e for e in evidence if e.get("problem_id") == primary.get("id")
        and e.get("in_scope", True) and e.get("source_n") in primary_refs
        and by_n[e["source_n"]].get("contour") in _NON_CHAT
        and e.get("date_kind") not in {"checked", "unknown"}
        and _date(e.get("date_iso"))
    ]
    if incidents:
        first_date = min(_date(e["date_iso"]) for e in incidents)
        require("primary_incident", {
            e["source_n"] for e in incidents if _date(e["date_iso"]) == first_date
        })
    elif primary_refs:
        require("primary_incident", {
            n for n in primary_refs if by_n[n].get("contour") in _NON_CHAT
        })

    summary = evidence_summary(list(by_n.values()), rows, qualification)
    company_positions = {r["source_n"] for r in summary["company_position"]}
    first_party_positions = {r["source_n"] for r in summary["first_party_company_position"]}
    cause_keys = {r["fact_key"] for r in summary["causes"]}
    company_causes = {r["source_n"] for r in summary["company_position"]
                      if r["fact_key"] in cause_keys}
    if company_positions:
        require("company_position", company_positions)
    if first_party_positions:
        # A secondary 'according to the developer' does not replace the company's
        # own exact first-person account when the research actually read one.
        require("first_party_company_position", first_party_positions)
    if company_causes:
        require("company_cause", company_causes)
    roles = {
        "company_position": (company_positions, 120),
        "first_party_company_position": (first_party_positions, 160),
        "cause": ({r["source_n"] for r in summary["causes"]} & primary_refs, 60),
        "buyer_primary": ({n for n in primary_refs if by_n[n].get("contour") in {
            "reviews", "communities"
        }}, 65),
        "official_card": ({n for n, s in by_n.items() if s.get("contour") == "official"
                           and any(_CARD_FACT.search(f.get("verbatim") or "")
                                   for f in facts_by_n[n])}, 35),
    }
    for row in summary["chronology"]:
        n = row["source_n"]
        if n in primary_refs:
            roles.setdefault("chronology:" + row["date"], (set(), 12))[0].add(n)
    redundant = {
        frozenset((a, b)) for a in by_n for b in by_n if a < b
        and _redundant(by_n[a], by_n[b], facts_by_n)
    }

    def quality(n):
        s = by_n[n]
        return (
            {"official": 4, "authorities": 4, "media": 3, "reviews": 2,
             "communities": 1}.get(s.get("contour"), 0),
            _date(s.get("evidence_date")),
            min(len(facts_by_n[n]), 10),
            -len(urlsplit(s.get("url") or "").path), -n,
        )

    def reward(chosen):
        return sum(weight for candidates, weight in roles.values() if candidates & chosen)

    # Exact bounded set-cover for indispensable proof, rather than greedy choices
    # that could consume six slots while another valid cover needs only three.
    covers, seen, best_size = [], set(), limit + 1

    def cover(chosen):
        nonlocal best_size
        state = frozenset(chosen)
        if state in seen or len(state) > best_size:
            return
        seen.add(state)
        missing = [values for values in required.values() if not values & state]
        if not missing:
            if len(state) < best_size:
                best_size = len(state)
                covers.clear()
            if len(covers) < 512:
                covers.append(state)
            return
        if len(state) >= min(limit, best_size):
            return
        max_gain = max(sum(n in values for values in missing) for n in by_n)
        lower_bound = (len(missing) + max_gain - 1) // max_gain
        if len(state) + lower_bound > min(limit, best_size):
            return
        smallest = min(missing, key=lambda values: (len(values), sorted(values)))
        for n in sorted(smallest, key=lambda n: (
            sum(n in values for values in missing), quality(n)
        ), reverse=True):
            cover(state | {n})

    cover(set())
    if not covers:
        raise EvidenceSelectionError("Обязательные доказательства не помещаются в PDF-источники")
    chosen = set(max(covers, key=lambda ns: (-len(ns), reward(ns), sorted(quality(n) for n in ns))))
    while len(chosen) < limit:
        pool = [n for n in by_n if n not in chosen and not any(
            frozenset((n, other)) in redundant for other in chosen
        )]
        if not pool:
            break
        previous = reward(chosen)
        n = max(pool, key=lambda n: (
            reward(chosen | {n}) - previous,
            by_n[n].get("contour") not in {by_n[k].get("contour") for k in chosen},
            quality(n),
        ))
        chosen.add(n)
    ordered = sorted(chosen, key=lambda n: (
        n not in stage_sources, n not in primary_refs, -reward({n}), -quality(n)[0], n
    ))
    mapping = {n: index for index, n in enumerate(ordered, 1)}
    selected = [{**by_n[n], "n": mapping[n], "registry_n": n} for n in ordered]
    projected = [{**deepcopy(f), "source_n": mapping[f["source_n"]]}
                 for f in rows if f["source_n"] in mapping]
    coverage = {
        "version": SELECTION_VERSION,
        "required": {name: sorted(ns & chosen) for name, ns in required.items()},
        "roles": {name: sorted(ns & chosen) for name, (ns, _) in roles.items()},
        "omitted_redundant": sorted(n for n in by_n if n not in chosen and any(
            frozenset((n, other)) in redundant for other in chosen
        )),
        "research_evidence_summary": summary,
        # Author-facing keys belong to the projected facts/source namespace.
        "evidence_summary": evidence_summary(selected, projected, qualification),
        "research_fact_count": len(rows), "pdf_fact_count": len(projected),
    }
    return Selection(selected, projected, mapping, coverage)


def buyer_quote_options(sources, facts, *, allowed_source_numbers=None):
    """Only exact, personal buyer speech becomes a copyable quotation option.

    Third-person news retellings and company statements are not a buyer's voice.
    Empty options are valid: the writer can use supported topics instead.
    """
    by_n = {s["n"]: s for s in sources if _readable(s)}
    result, seen = [], set()
    for fact in _facts(facts):
        n = fact["source_n"]
        source = by_n.get(n)
        if not source or (allowed_source_numbers is not None and n not in allowed_source_numbers):
            continue
        if source.get("contour") not in {"reviews", "communities"}:
            continue
        quote = fact.get("verbatim") or ""
        if not _grounded(fact, source) or not _ISSUE.search(quote):
            continue
        if _COMPANY_ROLE.search(fact.get("who_says") or "") or _REPORTING.search(quote):
            continue
        if fact.get("kind") not in {"quote", "claim"} or not _FIRST_PERSON.search(quote):
            continue
        # A forum/channel page date may describe its last post, not this voice.
        # Only the fact's validated individual date is an attribution date.
        dated = fact.get("date")
        span = date_span(dated)
        date_label = span[0].strftime("%d.%m.%Y") if span and span[0] == span[1] else None
        publisher = source.get("publisher") or "Публичное обсуждение"
        attribution = publisher + (", " + date_label if date_label else "")
        identity = {
            "version": QUOTE_VERSION,
            "source": _url(source.get("url")) or str(source.get("source_id") or n),
            "text": quote, "date": dated, "attribution": attribution,
        }
        key = "q-" + hashlib.sha256(json.dumps(
            identity, ensure_ascii=False, sort_keys=True
        ).encode()).hexdigest()[:20]
        if key in seen:
            continue
        seen.add(key)
        result.append({"quote_key": key, "text": quote, "refs": [n],
                       "attribution": attribution, "date": date_label})
    return result


def situation_contract(options):
    """Author chooses a key; it cannot rewrite quotation text, attribution or refs."""
    keys = tuple(option["quote_key"] for option in options)
    quote = create_model(
        "BuyerQuoteChoice", __base__=contract.Model,
        quote_key=(Literal[keys] if keys else str, ...),
    )
    buyers = create_model(
        "DraftBuyers", __base__=contract.Buyers,
        quotes=(Annotated[list[quote], Field(max_length=2 if keys else 0)], ...),
    )
    return create_model("SituationDraft", __base__=contract.Situation, buyers=(buyers, ...))


def resolve_situation(draft, options):
    """Insert the original exact fragment. Unknown/repeated keys are errors, not repairs."""
    value = situation_contract(options).model_validate(_data(draft)).model_dump()
    by_key = {option["quote_key"]: option for option in options}
    quotes, chosen = [], set()
    for choice in value["buyers"]["quotes"]:
        key = choice["quote_key"]
        if key in chosen or key not in by_key:
            raise ValueError("Неизвестный или повторный ключ цитаты покупателя")
        chosen.add(key)
        option = by_key[key]
        quotes.append({k: deepcopy(option[k]) for k in ("text", "attribution", "refs")})
    value["buyers"]["quotes"] = quotes
    return contract.Situation.model_validate(value)
