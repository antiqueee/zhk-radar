"""Conservative object grouping and source-backed timelines, without risk certification."""

import hashlib
import json
import re
import unicodedata
from datetime import UTC, date, datetime

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


def persist_findings(store, findings, documents, dataset="web"):
    initialize_sources(store)
    added, signals_added, discarded, dates_discarded = 0, 0, 0, 0
    accepted_ids = set()
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
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
                    r"^(корпус|корп\.?|дом|строение|стр\.?)\s+\w", finding.building, re.I
                )
            ):
                # Unproven building must not get attributed to a specific house.
                discarded += 1
                continue
            # Only normalize familiar inflections; never infer a region from the search query.
            finding = finding.model_copy(update={"region": source_region(finding.region, text)})
            event_date = supported_date(finding.event_date, finding.quote)
            dates_discarded += bool(finding.event_date and not event_date)
            ident = ("demo-web-" if dataset == "demo" else "web-") + object_key(
                finding, source["url"]
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
                    building=finding.building,
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
                )
            payload["urls"] = list(dict.fromkeys([*payload["urls"], source["url"]]))
            payload["last_seen_at"] = now()
            signal_id = hashlib.sha256(
                json.dumps(
                    [ident, source["url"], finding.quote, finding.event_type, event_date],
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
            previous = db.execute(
                "SELECT id FROM record_signals WHERE record_id=? "
                "AND json_extract(payload,'$.source_url')=? "
                "AND json_extract(payload,'$.quote')=? "
                "AND json_extract(payload,'$.event_type')=? "
                "AND json_extract(payload,'$.event_date') IS ? LIMIT 1",
                (ident, source["url"], finding.quote, finding.event_type, event_date),
            ).fetchone()
            count = 0
            if previous is None:
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
