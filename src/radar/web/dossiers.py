"""Conservative object grouping and source-backed timelines, without risk certification."""

import hashlib
import json
import re
import unicodedata
from datetime import UTC, date, datetime

from radar.web.record_identity import (
    RecordResolver,
    _developers,
    _project_locality,
    eis_url_id,
    identity_scope,
    project_name,
    unresolved_id,
)
from radar.web.sources import initialize_sources, primary_text
from radar.web.store import now

KINDS = {
    "commissioning_delay": "Перенос ввода",
    "handover_delay": "Перенос передачи ключей",
    "defects": "Дефекты и качество",
    "complaint": "Жалобы жителей",
    "supervision": "Надзор и предписания",
    "court": "Судебный спор",
    "other": "Требует классификации",
}


def normalized(value):
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return re.sub(r"[^\w]+", " ", value).strip()


def source_region(value, text):
    aliases = {
        "Москва": r"\bмоскв(?:а|е|у|ы|ой)\b",
        "Московская область": r"\bмосковск(?:ая|ой|ую)\s+област[ьи]\b",
        "Санкт-Петербург": r"\bсанкт[ -]петербург[аеу]?\b",
        "Ленинградская область": r"\bленинградск(?:ая|ой|ую)\s+област[ьи]\b",
    }
    for canonical, pattern in aliases.items():
        if re.fullmatch(pattern, value, re.I):
            return canonical if re.search(pattern, text, re.I) else "Не установлен"
    return value if value and value.casefold() in text.casefold() else "Не установлен"


def object_key(finding, source_url):
    name = re.sub(r"^(жк|жилой комплекс)\s+", "", normalized(finding.name))
    name = re.sub(r"\s+(жилые кварталы|жилой комплекс)$", "", name)
    region = normalized(finding.region)
    building = normalized(finding.building or "")
    building = re.sub(r"^корп\s", "корпус ", building)
    building = re.sub(r"^стр\s", "строение ", building)
    # Never merge unknown locations by name alone; keep separate, including unknown buildings.
    unknown = not region or region in {"не установлен", "не указан", "россия"}
    scope = source_url if unknown else region
    return hashlib.sha256(
        json.dumps([name, scope, building], ensure_ascii=False).encode()
    ).hexdigest()[:24]


def supported_date(value, quote):
    if not value or value > datetime.now(UTC).date():
        return None
    iso = value.isoformat()
    if iso in quote or value.strftime("%d.%m.%Y") in quote:
        return iso
    months = [
        "января",
        "февраля",
        "марта",
        "апреля",
        "мая",
        "июня",
        "июля",
        "августа",
        "сентября",
        "октября",
        "ноября",
        "декабря",
    ]
    pattern = rf"\b0?{value.day}\s+{months[value.month - 1]}\s+{value.year}\b"
    return iso if re.search(pattern, quote, re.I) else None


def _project_proof(incoming, source):
    name = project_name(incoming["name"])
    if not name:
        return "", ""
    title = source["title"]
    proof = incoming["quote"]
    target = f" {name} "
    if target in f" {normalized(title)} ":
        proof += " " + title
    elif target not in f" {normalized(proof)} ":
        return "", ""
    region = source_region(incoming["region"], proof)
    locality = _project_locality({"region": region})
    proof_text = f" {normalized(proof)} "
    developer = next((key for key in _developers(incoming) if f" {key} " in proof_text), "")
    return locality or "", developer


def _same_publication(db, resolver, incoming, source):
    name = project_name(incoming["name"])
    developers = _developers(incoming)
    if not name or not developers or not any(
        f" {name} " in f" {normalized(text)} " for text in (incoming["quote"], source["title"])
    ):
        return None, False
    ids = [
        ident for ident, row in resolver.rows.items()
        if source["url"] in (row.get("urls") or ())
    ]
    if not ids:
        return None, False
    building = identity_scope({"building": incoming["building"]})["buildings"]
    matching, archived = set(), set()
    for row in db.execute(
        "SELECT record_id,payload FROM record_signals WHERE record_id IN ("
        + ",".join("?" for _ in ids)
        + ") AND json_extract(payload,'$.source_url')=? AND json_extract(payload,'$.quote')=?",
        (*ids, source["url"], incoming["quote"]),
    ):
        signal = json.loads(row["payload"])
        stored = resolver.rows.get(signal.get("origin_record_id"), resolver.rows[row["record_id"]])
        if (
            project_name(stored.get("name")) != name
            or not _developers(stored) & developers
            or identity_scope({"building": signal.get("building")})["buildings"] != building
        ):
            continue
        ident = row["record_id"]
        canonical = resolver.canonical(ident)
        if ident == canonical:
            matching.add(ident)
        elif canonical and resolver.rows[canonical].get("granularity") == "project":
            archived.add(canonical)
    if len(matching) == 1:
        return next(iter(matching)), False
    # A merged member can retain a source that was excluded from the canonical project.
    return None, not matching and bool(archived)


def persist_findings(store, findings, documents, dataset="web"):
    initialize_sources(store)
    added, signals_added, discarded, dates_discarded = 0, 0, 0, 0
    accepted_ids = set()
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        resolver = RecordResolver(db, dataset)
        for finding in findings:
            if finding.source_index >= len(documents):
                discarded += 1
                continue
            source = documents[finding.source_index]
            text = primary_text(source["url"], source["text"])
            if normalized(finding.name) in {
                "многоквартирный дом",
                "жилой дом",
                "жилой комплекс",
                "жк",
                "дом",
                "новостройка",
            }:
                discarded += 1
                continue
            name = re.sub(r"^(жк|жилой комплекс)\s+", "", normalized(finding.name))
            if finding.quote not in text or name not in normalized(text):
                discarded += 1
                continue
            quote_text = normalized(finding.quote)
            # An explicit ЖК in the quote must refer to the proposed object, not a neighbour.
            if re.search(r"\bжк\b", quote_text) and name not in quote_text:
                discarded += 1
                continue
            if finding.building and (
                finding.building.casefold() not in finding.quote.casefold()
                or not re.match(
                    r"^(?:корпус|корп|дом|строение|стр)(?:\.\s*|\s+)(?:[№#]\s*)?\w",
                    finding.building,
                    re.I,
                )
            ):
                # Unproven building must not get attributed to a specific house.
                discarded += 1
                continue
            # Only normalize familiar inflections; never infer a region from the search query.
            finding = finding.model_copy(update={"region": source_region(finding.region, text)})
            event_date = supported_date(finding.event_date, finding.quote)
            dates_discarded += bool(finding.event_date and not event_date)
            incoming = finding.model_dump()
            base = ("demo-web-" if dataset == "demo" else "web-") + object_key(
                finding, source["url"]
            )
            resolution = resolver.resolve(incoming, source["url"], fallback_id=base)
            ident, archived = _same_publication(db, resolver, incoming, source)
            if archived and resolution.reason != "confirmed_house":
                discarded += 1
                continue
            if resolution.reason == "confirmed_house" or not ident:
                ident = resolution.record_id
            project_locality = None
            if dataset == "web" and finding.building and not ident:
                locality, developer = _project_proof(incoming, source)
                if locality and developer:
                    parent = resolver.resolve_project({**incoming, "project_locality": locality})
                    ident = parent.record_id
                    if ident:
                        project_locality = locality
                    if parent.candidates and not resolution.candidates:
                        resolution = parent
                    if (
                        not ident and not resolution.candidates and not eis_url_id(source["url"])
                        and project_name(finding.name) == name
                    ):
                        digest = hashlib.sha256(json.dumps(
                            ["project", name, locality, developer], ensure_ascii=False
                        ).encode()).hexdigest()[:24]
                        ident = "web-" + digest
                        project_locality = locality
            ident = ident or (
                unresolved_id(base, incoming, source["url"]) if resolution.candidates else base
            )
            accepted_ids.add(ident)
            old = db.execute("SELECT payload,revision FROM records WHERE id=?", (ident,)).fetchone()
            if old:
                payload = json.loads(old["payload"])
                if payload.get("verification") == "research_lead":
                    payload.update(summary=finding.summary, verification="source_backed_claims")
            else:
                payload = dict(
                    id=ident,
                    name=finding.name,
                    developer=finding.developer,
                    region=finding.region or "Не установлен",
                    summary=finding.summary,
                    building=None if project_locality else finding.building,
                    assessment=None,
                    urls=[],
                    claims={},
                    check=(
                        "Проверить корпус, вид срока, актуальность события и первичные документы."
                    ),
                    file="Поиск по публикациям",
                    sheet="Источники",
                    row=1,
                    verification="source_backed_claims",
                    granularity="building" if finding.building else "complex",
                    eisgs_id_hints=[],
                    first_seen_at=now(),
                    identity_scope=(
                        {"phases": [], "buildings": []} if project_locality else
                        identity_scope(incoming)
                    ),
                )
                if resolution.candidates:
                    payload["possible_duplicates"] = list(resolution.candidates)
                    payload["duplicate_check"] = resolution.reason
            if project_locality:
                payload["granularity"] = "project"
                payload.setdefault("project_locality", project_locality)
            payload["urls"] = list(dict.fromkeys([*payload["urls"], source["url"]]))
            payload["last_seen_at"] = now()
            scoped_project = payload.get("granularity") == "project"
            scope = identity_scope({"building": finding.building})["buildings"]
            signal_id = hashlib.sha256(
                json.dumps(
                    [ident, source["url"], finding.quote, finding.event_type, event_date]
                    + ([scope] if scoped_project and scope else []),
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
            signal = dict(
                summary=finding.summary,
                quote=finding.quote,
                event_type=finding.event_type,
                event_date=event_date,
                published_at=source.get("published_at"),
                source_title=source["title"],
                source_url=source["url"],
                source_kind=source["source_kind"],
                source_id=source["source_id"],
                topics=finding.topics,
                building=finding.building,
                limitation="Цитата есть в тексте. Утверждение источника требует проверки.",
            )
            if source.get("search_provider") == "perplexity":
                signal["search_provider"] = "perplexity"
            previous_rows = db.execute(
                "SELECT json_extract(payload,'$.building') AS building FROM record_signals "
                "WHERE record_id=? "
                "AND json_extract(payload,'$.source_url')=? "
                "AND json_extract(payload,'$.quote')=? "
                "AND json_extract(payload,'$.event_type')=? "
                "AND json_extract(payload,'$.event_date') IS ?",
                (ident, source["url"], finding.quote, finding.event_type, event_date),
            )
            previous = any(
                not scoped_project or identity_scope({"building": row["building"]})["buildings"]
                == scope for row in previous_rows
            )
            count = 0
            if not previous:
                count = db.execute(
                    "INSERT OR IGNORE INTO record_signals VALUES(?,?,?,?,?)",
                    (
                        signal_id,
                        ident,
                        source["source_id"],
                        json.dumps(signal, ensure_ascii=False),
                        now(),
                    ),
                ).rowcount
            signals_added += count
            if old:
                if count:
                    db.execute(
                        "UPDATE records SET payload=?,updated_at=?,revision=revision+1 WHERE id=?",
                        (json.dumps(payload, ensure_ascii=False), now(), ident),
                    )
            else:
                db.execute(
                    "INSERT INTO records(id,dataset,payload,situation,updated_at) "
                    "VALUES(?,?,?,?,?)",
                    (
                        ident,
                        dataset,
                        json.dumps(payload, ensure_ascii=False),
                        "после_заселения"
                        if finding.event_type == "defects"
                        or set(finding.topics) & {"качество_ук", "протечки"}
                        else "приёмка"
                        if "приёмка" in finding.topics
                        else "сроки",
                        now(),
                    ),
                )
                added += 1
            resolver.add(ident, payload)
            if count:
                db.execute(
                    "INSERT OR IGNORE INTO evidence"
                    "(record_id,title,url,quote,source_text,created_at) VALUES(?,?,?,?,?,?)",
                    (ident, source["title"][:160], source["url"], finding.quote, text, now()),
                )
                db.execute(
                    "INSERT INTO events(record_id,action,detail,created_at) VALUES(?,?,?,?)",
                    (ident, "source", "Добавлена публикация: " + source["title"][:120], now()),
                )
    return dict(
        found=len(accepted_ids),
        added=added,
        signals_added=signals_added,
        discarded=discarded,
        dates_discarded=dates_discarded,
        record_ids=sorted(accepted_ids),
    )


def dossier(store, ident):
    initialize_sources(store)
    with store.connect() as db:
        rows = db.execute(
            "SELECT s.*,p.payload AS page FROM record_signals s "
            "LEFT JOIN source_pages p ON p.id=s.source_id WHERE record_id=? "
            "ORDER BY s.created_at,s.id",
            (ident,),
        ).fetchall()
    signals = []
    sources = {}
    topics = {}
    for row in rows:
        signal = json.loads(row["payload"])
        signal.update(
            id=row["id"], first_seen_at=row["created_at"], kind_label=KINDS[signal["event_type"]]
        )
        page = json.loads(row["page"]) if row["page"] else {}
        sources[signal["source_id"]] = {
            key: page.get(key)
            for key in [
                "id",
                "url",
                "final_url",
                "title",
                "status",
                "published_at",
                "fetched_at",
                "truncated",
            ]
        }
        sources[signal["source_id"]]["kind"] = signal["source_kind"]
        for topic in set(signal["topics"]):
            topics[topic] = topics.get(topic, 0) + 1
        signals.append(signal)
    signals.sort(
        key=lambda s: (s["event_date"] or s["published_at"] or s["first_seen_at"][:10], s["id"])
    )
    today = datetime.now(UTC).date()
    dated = [s["event_date"] for s in signals if s["event_date"]]
    return {
        "signals": signals,
        "sources": list(sources.values()),
        "topics": topics,
        "full_pages": sum(s.get("kind") == "page" for s in sources.values()),
        "snippets": sum(s.get("kind") == "snippet" for s in sources.values()),
        "last_event_date": max(dated) if dated else None,
        "old_event": bool(dated and (today - date.fromisoformat(max(dated))).days > 90),
        "limitation": "Несколько публикаций могут пересказывать один источник; "
        "число публикаций не равно числу независимых подтверждений.",
    }
