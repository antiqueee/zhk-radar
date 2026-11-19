"""One grounded gap round inside the existing search/read budget; no model or SDK calls."""

import base64
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, unquote, urlsplit

from radar.reports.fact_spans import source_span
from radar.web.chats import REDACTION_VERSION, redact
from radar.web.configuration import configuration
from radar.web.discovery import yandex
from radar.web.model_gateway import routed_client
from radar.web.public_communities import is_public_preview, public_preview_url
from radar.web.record_locks import RecordLocks
from radar.web.report_evidence import date_span
from radar.web.report_search import (
    VERSION as SEARCH_VERSION,
)
from radar.web.report_search import (
    aliases_for,
    candidate_relevance,
    contour_for,
    get_research,
    research_url,
    search_config,
)
from radar.web.research_support import company_position
from radar.web.sources import (
    PRIMARY_TEXT_VERSION,
    Page,
    fetch_page,
    initialize_sources,
    is_access_page,
    load_page,
    primary_text,
)
from radar.web.store import now
from radar.web.workflow import cached, initialize

VERSION = "grounded-gap-round-v2"
FOLLOWUP_LOCKS = RecordLocks()
DEFAULT_TERMS = {
    "cause": "причины переноса",
    "company_position": "комментарий застройщика",
    "current_status": "ход строительства",
    "phase_deadlines": "срок ввода выдача ключей",
}
_DELAY = re.compile(r"перен[её]с|перенос|просроч|задерж|сорван\w* срок", re.I)
_CAUSE = re.compile(r"из-за|в связи с|объясн\w*|причин\w*|обусловлен", re.I)
_NO_CAUSE = re.compile(
    r"причин\w*[^.]{0,45}(?:неизвест|не извест|не раскрыт)|не (?:объясн|назва|сообщ|указ)|"
    r"(?:прос|требу|необходим)[^.]{0,45}(?:объясн|причин)",
    re.I,
)
_NO_POSITION = re.compile(r"не прокомментир|без комментар|нет (?:официального )?комментар", re.I)
_STATUS = re.compile(
    r"строительств\w*|строится|ведутся работы|готовност\w*|введ[её]н|ключи выдают", re.I
)
_PLANNED = re.compile(r"планиру|планов|срок (?:ввода|сдачи)|ориентир|обеща", re.I)
_INTENTS = {
    "cause": re.compile(r"причин|из-за|объясн|геолог|подрядчик", re.I),
    "company_position": re.compile(r"коммент|поясн|объясн|сообщ|заяв|пресс-служб", re.I),
    "current_status": _STATUS,
    "phase_deadlines": re.compile(r"ввод|сдач|передач|ключ|декларац|очеред|корпус", re.I),
}
_LINKED_TOPIC = re.compile(
    r"\b(?:обращен|жалоб|перенос|срок|дольщик|застройщик|проверк|готовност|деклараци)\w*",
    re.I,
)
_ARTICLE_LINK = re.compile(
    r"/(?:news|articles?|publications?|posts?|documents?|новости|документ\w*|объект)"
    r"/[^/]+|/(?:19|20)\d{2}/\d{1,2}/\d{1,2}/[^/]+",
    re.I,
)
_FORUM_REDIRECT = ("pronovostroy.ru", "/go.php")
_LINKED_READ_LIMIT = 2


def _dict(value):
    return value.model_dump() if hasattr(value, "model_dump") else value


def _facts(value):
    value = _dict(value)
    return [_dict(f) for f in (value.get("facts", []) if isinstance(value, dict) else value or [])]


def _known_brand(record):
    value = str(record.get("developer") or "").strip()
    return "" if re.search(r"не установлен|неизвест|unknown", value, re.I) else value


def _host(url):
    return (urlsplit(url).hostname or "").casefold().removeprefix("www.")


def _contour(record, url, config):
    host = _host(url)
    if any(host == d or host.endswith("." + d) for d in record.get("official_domains", [])):
        return "official"
    return contour_for(url, plan_config=config)


def _grounded_pairs(sources, facts):
    by_n = {s.get("n"): s for s in sources}
    for fact in _facts(facts):
        source = by_n.get(fact.get("source_n"))
        if not source or source.get("kind", source.get("source_kind")) == "snippet":
            continue
        if source.get("read_status", "loaded") != "loaded":
            continue
        if fact.get("verbatim") and source_span(fact["verbatim"], source.get("text", "")):
            yield fact, source


def _linked_target(raw, origin):
    url = research_url(raw)
    parts = urlsplit(url)
    wrapped = (_host(url), parts.path) == _FORUM_REDIRECT
    if wrapped:
        if _host(origin) != _FORUM_REDIRECT[0]:
            return None, False
        values = parse_qs(parts.query).get("url", [])
        if len(values) != 1 or len(values[0]) > 4096:
            return None, False
        url = research_url(base64.b64decode(values[0], validate=True).decode())
    parts = urlsplit(url)
    if not _ARTICLE_LINK.search(unquote(parts.path)):
        return None, False
    return url, wrapped


def linked_primary_sources(record, sources, facts, documents, config):
    """Reserve a few reads for primary pages actually referenced by grounded material."""
    grounded_urls, loaded = set(), set()
    for fact, source in _grounded_pairs(sources, facts):
        if source.get("contour") not in {"reviews", "media"} or not source.get("url"):
            continue
        if _LINKED_TOPIC.search(fact["verbatim"]):
            grounded_urls.add(research_url(source["url"]))
    for row in [*sources, *documents]:
        if row.get("url") and row.get("text") and row.get("read_status", "loaded") == "loaded":
            loaded.add(research_url(row["url"]))

    chosen = {}
    aliases = aliases_for(record)
    for document in [*documents, *sources]:
        if (
            not document.get("url") or not document.get("links")
            or document.get("read_status", "loaded") != "loaded"
        ):
            continue
        origin = research_url(document["url"])
        if origin not in grounded_urls:
            continue
        source_text = str(document.get("text") or "").casefold()
        for raw in document["links"]:
            if not isinstance(raw, str) or len(raw) > 5000:
                continue
            try:
                target, wrapped = _linked_target(raw, origin)
            except (ValueError, TypeError):
                continue
            if not target or target in loaded or target in chosen or _host(target) == _host(origin):
                continue
            contour = _contour(record, target, config)
            if contour not in {"media", "official", "authorities"}:
                continue
            host_parts = _host(target).split(".")
            publisher = host_parts[-2] if len(host_parts) >= 2 else ""
            # The supported forum masks article URLs in visible text. Require the
            # displayed publisher as well as the exact decoded outbound article.
            visible_reference = target.casefold() in source_text or (
                wrapped and len(publisher) >= 5
                and re.search(r"(?<!\w)" + re.escape(publisher) + r"(?!\w)", source_text)
            )
            entity_in_url = candidate_relevance(record, {"url": target}, aliases)["accepted"]
            if not (visible_reference or entity_in_url):
                continue
            chosen[target] = {"url": target, "contour": contour, "linked_from": origin}
            break
        if len(chosen) >= _LINKED_READ_LIMIT:
            break
    return list(chosen.values())


def public_candidates(record, sources):
    """Hints for the separate read-only SDK binder, never proof that a chat was read."""
    found = {}
    rows = [*sources, {"url": "", "links": record.get("urls", []), "title": record["name"]}]
    for source in rows:
        for raw in [source.get("url", ""), *(source.get("links") or [])]:
            try:
                url = public_preview_url(raw, "Открытый чат " + record["name"])
                if not url or (urlsplit(url).hostname or "").casefold() != "t.me":
                    continue
                path = urlsplit(url).path.strip("/").split("/")
                slug = path[1] if path[0] == "s" else path[0]
                canonical = "https://t.me/" + slug.casefold()
            except (ValueError, IndexError, TypeError):
                continue
            row = found.setdefault(
                canonical,
                {
                    "url": canonical,
                    "evidence_urls": [],
                    "requires_inspection": True,
                    "hint": "Публичная ссылка; название и соответствие объекту ещё проверяет SDK",
                },
            )
            if source.get("url") and source["url"] not in row["evidence_urls"]:
                row["evidence_urls"].append(source["url"])
    return list(found.values())[:4]


def plan_gaps(record, sources, facts, qualification=None, *, today=None, plan_config=None):
    """Missing evidence requests, not a replacement for factual qualification."""
    today = today or datetime.now(UTC).date()
    cfg = (plan_config or search_config()).get("followup", {})
    grounded = list(_grounded_pairs(sources, facts))
    brand = _known_brand(record)
    delay = any(_DELAY.search(f["text"]) for f, _ in grounded)
    cause = any(_CAUSE.search(f["text"]) and not _NO_CAUSE.search(f["text"]) for f, _ in grounded)
    company = any(
        company_position(
            f,
            s,
            passport={
                "official_name": record["name"],
                "aliases": record.get("aliases", []),
                "developer_brand": brand,
            },
        )
        for f, s in grounded
    )
    status = False
    for fact, source in grounded:
        if source.get("contour") not in {"official", "media", "authorities"}:
            continue
        span = date_span(fact.get("date") or source.get("published_at"))
        if (
            span
            and span[1] <= today
            and (today - span[1]).days <= int(cfg.get("status_max_age_days", 90))
        ):
            status |= bool(_STATUS.search(fact["text"]) and not _PLANNED.search(fact["text"]))
    phases = (qualification or {}).get("passport", {}).get("phases", [])
    phase_dates = bool(phases) and all(
        p.get("completion_date") and p.get("keys_date") for p in phases
    )
    gaps = [
        key
        for key, needed in [
            ("cause", delay and not cause),
            ("company_position", not company),
            ("current_status", not status),
            ("phase_deadlines", delay and not phase_dates),
        ]
        if needed
    ]
    aliases = aliases_for(record, qualification)
    official = [d for d in record.get("official_domains", []) if re.fullmatch(r"[\w.-]+", d)]
    terms = {**DEFAULT_TERMS, **cfg.get("terms", {})}
    cutoff = (today - timedelta(days=int(cfg.get("status_max_age_days", 90)))).strftime("%Y%m%d")
    queries = []
    for index, gap in enumerate(gaps):
        alias = re.sub(r'["\n\r]', " ", aliases[index % len(aliases)]).strip()
        anchor = f'"{alias}"'
        if gap in {"company_position", "current_status"} and official:
            anchor = f"site:{official[0]} {anchor}"
        elif brand and brand.casefold() not in alias.casefold():
            anchor += ' "' + re.sub(r'["\n\r]', " ", brand) + '"'
        else:
            anchor += " " + str(record.get("region") or "")
        query = f"{anchor} {terms[gap]}".strip()
        if gap == "current_status":
            query += " date:>=" + cutoff
        queries.append({"gap": gap, "query": query, "period": "all"})
    return {
        "version": VERSION,
        "gaps": gaps,
        "queries": queries[:4],
        "public_candidates": public_candidates(record, sources),
        "grounded_facts": len(grounded),
    }


def _read(store, row, record, aliases, reader):
    with store.connect() as db:
        saved = db.execute(
            "SELECT payload FROM source_pages WHERE url=? AND fetched_at>=? "
            "AND json_extract(payload,'$.status')='loaded' ORDER BY fetched_at DESC LIMIT 1",
            (row["url"], (datetime.now(UTC) - timedelta(days=7)).isoformat()),
        ).fetchone()
    payload = json.loads(saved[0]) if saved else None
    if payload and (not is_public_preview(row["url"]) or payload.get("messages")):
        page, hit = Page(**payload), True
    else:
        page, hit = load_page(store, row["url"], reader)
    text = primary_text(page.final_url or row["url"], page.text)
    relevance = candidate_relevance(record, {**row, "title": page.title, "text": text}, aliases)
    status = page.status
    if is_access_page(page.title, text):
        status = "protected"
    if status == "loaded" and not relevance["accepted"]:
        status = "out_of_scope"
    if status == "loaded" and row.get("linked_from") and (
        _host(page.final_url or row["url"]) != _host(row["url"])
    ):
        status = "out_of_scope"
    messages = page.messages if status == "loaded" else None
    return {
        **row,
        "title": page.title or row.get("title", ""),
        "text": redact(text, []) if status == "loaded" else "",
        "read_status": status,
        "source_kind": "chat" if messages else "page" if status == "loaded" else "snippet",
        "source_id": page.id or hashlib.sha256((row["url"] + text).encode()).hexdigest(),
        "published_at": page.published_at,
        "checked_at": page.fetched_at or now(),
        "page_relevance": relevance,
        "truncated": page.truncated,
        "links": page.links or [],
        **(
            {"messages": [{**m, "text": redact(m["text"], [])} for m in messages]}
            if messages
            else {}
        ),
    }, hit


def ensure_followup(
    store,
    ident,
    run_id,
    *,
    sources,
    facts,
    qualification=None,
    client=None,
    today=None,
    searcher=yandex,
    reader=fetch_page,
):
    """Return new documents only. Caller merges/extracts once and keeps the ordinary gate."""
    today = today or datetime.now(UTC).date()
    record, config = store.get(ident), search_config()
    plan = plan_gaps(record, sources, facts, qualification, today=today, plan_config=config)
    base = get_research(store, ident) or {}
    links = linked_primary_sources(record, sources, facts, base.get("documents", []), config)
    if links:
        plan["linked_sources"] = links
    limits, extra = config["budget"], config.get("followup", {})
    used_search = int(base.get("search_calls", 0))
    used_pages = max(int(base.get("page_reads", 0)), len(base.get("documents", [])))
    max_search = min(
        4,
        int(extra.get("reserve_search_calls", 0)),
        max(0, min(40, limits["search_calls"]) - used_search),
    )
    max_pages = min(
        4,
        int(extra.get("reserve_page_reads", 0)),
        max(0, min(25, limits["page_reads"]) - used_pages),
    )
    signature = hashlib.sha256(
        json.dumps(
            [
                VERSION,
                PRIMARY_TEXT_VERSION,
                REDACTION_VERSION,
                record,
                plan,
                sorted(json.dumps(f, sort_keys=True, ensure_ascii=False) for f in _facts(facts)),
                sorted(
                    (s.get("url", ""), hashlib.sha256(s.get("text", "").encode()).hexdigest())
                    for s in sources
                ),
                config,
                today.toordinal() // 7,
            ],
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    initialize(store)
    initialize_sources(store)
    with FOLLOWUP_LOCKS.for_record(store, ident):
        with store.connect() as db:
            row = db.execute(
                "SELECT payload FROM workflow_cache WHERE key=?", (signature,)
            ).fetchone()
        if row:
            saved = json.loads(row[0])
            return {
                **saved,
                "cached": True,
                "public_candidates": public_candidates(
                    record, [*sources, *base.get("documents", []), *saved.get("documents", [])]
                ),
            }
        result = {
            "version": VERSION,
            "signature": signature,
            "created_at": now(),
            "plan": plan,
            "documents": [],
            "errors": [],
            "search_calls": 0,
            "page_reads": 0,
            "cache_hits": 0,
            "public_candidates": public_candidates(record, [*sources, *base.get("documents", [])]),
            "coverage": {
                "requested_gaps": plan["gaps"],
                "gaps_resolved": False,
                "status": "no_gaps" if not (plan["gaps"] or links) else "budget_exhausted",
            },
        }
        if int(extra.get("max_rounds", 0)) < 1:
            result["coverage"]["status"] = "disabled"
            return result
        cfg = configuration(store.root)
        if record.get("dataset") == "demo":
            result["coverage"]["status"] = "demo"
            return result
        searches = plan["queries"][:max_search]
        if not max_pages or not (searches or links):
            return result
        if not cfg.get("YANDEX_SEARCH_API_KEY") or not cfg.get("YANDEX_FOLDER_ID"):
            if not links:
                result["coverage"]["status"] = "search_unconfigured"
                return result
            searches = []
        owns = client is None and bool(searches)
        if owns:
            client = routed_client(cfg, timeout=60)
        try:
            known = {research_url(s["url"]) for s in sources if s.get("url")}
            known.update(research_url(d["url"]) for d in base.get("documents", []) if d.get("url"))
            aliases = aliases_for(record, qualification)
            pool = {}
            bucket = (today - timedelta(days=today.toordinal() % 7)).isoformat()
            for query in searches:
                result["search_calls"] += 1
                try:
                    rows, hit = cached(
                        store,
                        [SEARCH_VERSION, "search", bucket, query["query"], "all"],
                        lambda q=query: searcher(client, cfg, q["query"], "all"),
                        run_id,
                        "search",
                    )
                    result["cache_hits"] += bool(hit)
                except ValueError:
                    result["errors"].append(
                        {
                            "stage": "search",
                            "gap": query["gap"],
                            "detail": "Адресный поиск не получен",
                        }
                    )
                    continue
                for raw in rows:
                    try:
                        url = public_preview_url(raw["url"], raw.get("title", ""))
                        if not url:
                            continue
                        url = research_url(url)
                    except (ValueError, KeyError):
                        continue
                    relevance = candidate_relevance(record, {**raw, "url": url}, aliases)
                    if url in known or not relevance["accepted"]:
                        continue
                    pool.setdefault(
                        url,
                        {
                            **raw,
                            "url": url,
                            "gap": query["gap"],
                            "relevance": relevance,
                            "contour": _contour(record, url, config),
                        },
                    )
            ordered = sorted(
                pool.values(),
                key=lambda r: (
                    not bool(
                        _INTENTS[r["gap"]].search(r.get("title", "") + " " + r.get("text", ""))
                    ),
                    -r["relevance"]["score"],
                ),
            )
            selected, selected_gaps = [], set()
            for row in ordered:
                if row["gap"] not in selected_gaps:
                    selected.append(row)
                    selected_gaps.add(row["gap"])
            selected_urls = {r["url"] for r in selected}
            ordered = selected + [r for r in ordered if r["url"] not in selected_urls]
            linked_urls = {r["url"] for r in links}
            ordered = links + [r for r in ordered if r["url"] not in linked_urls]
            for row in ordered[:max_pages]:
                document, hit = _read(store, row, record, aliases, reader)
                result["documents"].append(document)
                result["page_reads"] += 1
                result["cache_hits"] += bool(hit)
            result["public_candidates"] = public_candidates(
                record, [*sources, *base.get("documents", []), *result["documents"]]
            )
            result["coverage"].update(
                status="partial"
                if result["errors"]
                else "awaiting_fact_check"
                if result["documents"]
                else "no_new_documents",
                loaded=sum(d["read_status"] == "loaded" for d in result["documents"]),
                unavailable=[
                    {"url": d["url"], "read_status": d["read_status"]}
                    for d in result["documents"]
                    if d["read_status"] != "loaded"
                ],
                total_search_calls=used_search + result["search_calls"],
                total_page_reads=used_pages + result["page_reads"],
            )
            with store.connect() as db:
                db.execute(
                    "INSERT OR IGNORE INTO workflow_cache VALUES(?,?,?)",
                    (signature, json.dumps(result, ensure_ascii=False), now()),
                )
            return result
        finally:
            if owns:
                client.close()
