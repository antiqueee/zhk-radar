"""Describe collected evidence without equating publications with independent proof."""

import json
from collections import defaultdict
from difflib import SequenceMatcher
from urllib.parse import urlsplit

from radar.web.dossiers import normalized
from radar.web.sources import canonical_url


def summarize(items):
    urls, hosts, quotes = set(), set(), []
    for item in items:
        raw_url = item.get("source_url") or item.get("url")
        quote = normalized(item.get("quote", ""))
        if not raw_url or (not quote and not item.get("loaded_document")):
            continue
        try:
            url = canonical_url(raw_url)
        except ValueError:
            continue
        urls.add(url)
        hosts.add((urlsplit(url).hostname or "").removeprefix("www."))
        if not quote:
            # A read page is coverage, not a validated quotation or independent proof.
            continue
        # Identical/near-identical quotations remain one group even on different sites.
        if not any(
            quote in old
            or old in quote
            or SequenceMatcher(None, quote, old, autojunk=False).ratio() >= 0.85
            for old in quotes
        ):
            quotes.append(quote)
    multiple = len(hosts) >= 2 and len(quotes) >= 2
    return {
        "publications": len(urls),
        "sites": len(hosts),
        "quote_groups": len(quotes),
        "label": "Подборка источников"
        if multiple
        else (
            "Одиночный источник"
            if len(urls) == 1
            else "Нужны другие источники"
            if urls
            else "Источники ещё не собраны"
        ),
        "multiple": multiple,
        "note": "Разные сайты могут перепечатывать одну новость. "
        "Независимость и достоверность утверждений не подтверждены автоматически.",
    }


def _tables(db):
    return {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('record_signals','evidence','research_runs','report_research',"
            "'brand_analytics_messages','brand_analytics_matches')"
        )
    }


def _documents(payload):
    try:
        documents = json.loads(payload).get("documents", [])
    except (TypeError, ValueError, AttributeError):
        return []
    return [
        {"url": document.get("url"), "loaded_document": True}
        for document in documents
        if isinstance(document, dict)
        and document.get("read_status") == "loaded"
        and document.get("source_kind") not in {"snippet", "protected"}
        and not document.get("protected")
        and str(document.get("text") or "").strip()
    ]


_BRAND_COVERAGE = (
    "SELECT x.record_id,m.url,1 AS loaded_document FROM brand_analytics_matches x "
    "JOIN brand_analytics_messages m ON m.id=x.message_id "
    "WHERE m.complete=1 AND substr(m.published_at,1,10)<=date('now')"
)


def all_records(store):
    grouped = defaultdict(list)
    with store.connect() as db:
        tables = _tables(db)
        if "record_signals" in tables:
            for row in db.execute("SELECT record_id,payload FROM record_signals"):
                grouped[row["record_id"]].append(json.loads(row["payload"]))
        # Manual evidence is also tied explicitly to an object.
        if "evidence" in tables:
            for row in db.execute("SELECT record_id,url,quote FROM evidence"):
                grouped[row["record_id"]].append(dict(row))
        if "research_runs" in tables:
            for row in db.execute("SELECT record_id,payload FROM research_runs"):
                grouped[row["record_id"]].extend(json.loads(row["payload"]).get("answers", []))
        if "report_research" in tables:
            for row in db.execute("SELECT record_id,payload FROM report_research"):
                grouped[row["record_id"]].extend(_documents(row["payload"]))
        if {"brand_analytics_messages", "brand_analytics_matches"} <= tables:
            for row in db.execute(_BRAND_COVERAGE):
                grouped[row["record_id"]].append(dict(row))
    return {ident: summarize(items) for ident, items in grouped.items()}


def for_record(store, ident):
    """Card reads are bounded to this object, irrespective of registry size."""
    with store.connect() as db:
        tables = _tables(db)
        items = []
        if "record_signals" in tables:
            items.extend(
                json.loads(r["payload"])
                for r in db.execute(
                    "SELECT payload FROM record_signals WHERE record_id=?", (ident,)
                )
            )
        if "evidence" in tables:
            items.extend(
                dict(r)
                for r in db.execute("SELECT url,quote FROM evidence WHERE record_id=?", (ident,))
            )
        if "research_runs" in tables:
            for row in db.execute("SELECT payload FROM research_runs WHERE record_id=?", (ident,)):
                items.extend(json.loads(row["payload"]).get("answers", []))
        if "report_research" in tables:
            for row in db.execute(
                "SELECT payload FROM report_research WHERE record_id=?", (ident,)
            ):
                items.extend(_documents(row["payload"]))
        if {"brand_analytics_messages", "brand_analytics_matches"} <= tables:
            items.extend(dict(row) for row in db.execute(
                _BRAND_COVERAGE + " AND x.record_id=?", (ident,),
            ))
    return summarize(items)
