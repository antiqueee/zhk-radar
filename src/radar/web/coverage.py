"""Describe collected evidence without equating publications with independent proof."""

import json
from collections import defaultdict
from difflib import SequenceMatcher
from urllib.parse import urlsplit

from radar.web.dossiers import normalized
from radar.web.sources import canonical_url, initialize_sources


def summarize(items):
    urls, hosts, quotes = set(), set(), []
    for item in items:
        raw_url = item.get("source_url") or item.get("url")
        quote = normalized(item.get("quote", ""))
        if not raw_url or not quote:
            continue
        try:
            url = canonical_url(raw_url)
        except ValueError:
            continue
        urls.add(url)
        hosts.add((urlsplit(url).hostname or "").removeprefix("www."))
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


def all_records(store):
    initialize_sources(store)
    grouped = defaultdict(list)
    with store.connect() as db:
        for row in db.execute("SELECT record_id,payload FROM record_signals"):
            grouped[row["record_id"]].append(json.loads(row["payload"]))
        # Manual evidence is also tied explicitly to an object.
        for row in db.execute("SELECT record_id,url,quote FROM evidence"):
            grouped[row["record_id"]].append(dict(row))
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_runs'"
        ).fetchone()
        if exists:
            for row in db.execute("SELECT record_id,payload FROM research_runs"):
                grouped[row["record_id"]].extend(json.loads(row["payload"]).get("answers", []))
    return {ident: summarize(items) for ident, items in grouped.items()}
