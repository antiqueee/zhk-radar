"""Bounded public-chat evidence for a report; no joins, sending or member lookup."""

import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from radar.web import screening
from radar.web.chats import redact
from radar.web.dossiers import normalized
from radar.web.public_communities import public_preview_url
from radar.web.report_followup import public_candidates
from radar.web.report_search import aliases_for, candidate_relevance
from radar.web.sources import primary_text
from radar.web.telegram import TelegramLink

VERSION = "report-public-chats-v2"
MAX_CHATS = 2
MAX_INSPECTIONS = 3
MAX_MESSAGES = 1000
MODEL_CHAR_BUDGET = 40000
SIGNAL = re.compile(
    r"срок|ключ|перенос|дольщик|коллектив|претензи|митинг|пикет|собрани|"
    r"встреч|подпис|надзор|прокуратур|при[её]мк|компенсац|неустойк",
    re.I,
)


def _public_url(value):
    try:
        parts = urlsplit(value)
        if (
            parts.hostname != "t.me"
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            return None
        selected = public_preview_url(value, "Открытый чат")
        if not selected:
            return None
        path = parts.path.strip("/").split("/")
        if path and path[0] == "s":
            path = path[1:]
        return TelegramLink(url="https://t.me/" + path[0]).url if path else None
    except (ValueError, TypeError, IndexError):
        return None


def _instant(value):
    try:
        stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
        return stamp.astimezone(UTC) if stamp and stamp.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def _scope(value):
    roman = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "x": "10"}
    result = set()
    for match in re.finditer(
        r"\b(корпус|корп|литер|очередь|этап)\s+(\d+[а-яa-z]?|[ivx]+)\b|"
        r"\b(\d+|[ivx]+)\s+(очередь)\b",
        normalized(value),
        re.I,
    ):
        kind, number = (match[1], match[2]) if match[1] else (match[4], match[3])
        result.add(("корпус" if kind == "корп" else kind, roman.get(number, number)))
    return result


def _match(record, metadata, candidate):
    required = _scope(
        " ".join(
            str(x or "")
            for x in [
                record.get("name"),
                record.get("building"),
                (record.get("report_scope") or {}).get("phase"),
            ]
        )
    )
    building = normalized(str(record.get("building") or ""))
    if re.fullmatch(r"\d+[а-яa-z]?|[ivx]+", building):
        required |= _scope("корпус " + building)
    actual = _scope(metadata.get("title", "") + " " + metadata.get("description", ""))
    if required and not required <= actual:
        return False, "Очередь или корпус не подтверждены описанием публичного чата"
    for name in dict.fromkeys([record["name"], *(record.get("aliases") or [])]):
        accepted, reason = screening.matches({**record, "name": name}, metadata)
        if accepted:
            return True, reason
        if reason == "Регион не подтверждён описанием чата":
            # A binding href may supply an omitted location, never replace a
            # different location explicitly added to the matching chat title.
            rest = " " + normalized(metadata.get("title", "")) + " "
            for known in [record["name"], *(record.get("aliases") or []), record.get("developer")]:
                if known:
                    rest = rest.replace(" " + normalized(known) + " ", " ")
            rest = re.sub(
                r"\b(?:жк|жилой|комплекс|чат|канал|группа|соседи|соседей|собственник\w*|"
                r"дольщик\w*|жител\w*|корпус|корп|дом|литер|очередь|этап|\d+|[ivx]+)\b",
                " ",
                rest,
            )
            if rest.strip():
                continue
            for context in candidate.get("contexts", []):
                accepted, reason = screening.matches({**record, "name": name}, metadata, context)
                if accepted:
                    return True, reason
    return False, "Название, регион или корпус не подтверждены описанием публичного чата"


def _candidates(record, research):
    documents = (research or {}).get("documents", [])
    raw_urls = [*(record.get("urls") or [])]
    for document in documents:
        raw_urls.extend([document.get("url"), *(document.get("links") or [])])
    allowed = {url for raw in raw_urls if (url := _public_url(raw))}
    candidates = {
        r["url"]: {**r, "contexts": []}
        for r in public_candidates(record, documents)
        if r["url"] in allowed
    }
    for document in documents:
        if (
            document.get("read_status", "loaded") != "loaded"
            or document.get("source_kind") == "snippet"
        ):
            continue
        text = primary_text(document.get("url") or "", document.get("text") or "")
        proof = {**document, "text": text}
        if not text or not candidate_relevance(record, proof, aliases_for(record))["accepted"]:
            continue
        # A URL mentioned in prose alone is not a verified link to the chat.
        for href in document.get("links") or []:
            url = _public_url(href)
            if url in candidates:
                candidates[url]["contexts"].append(
                    {
                        "verified_url": url,
                        "source_url": document["url"],
                        "context_text": document.get("title", "") + "\n" + text,
                    }
                )
    return candidates


def _documents(store, ident, links, checked_at):
    """Keep whole selected messages, separately count the complete stored window."""
    at = _instant(checked_at)
    windows, selections, candidates = {}, {}, []
    with store.connect() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='local_messages'").fetchone()
        if not exists:
            return [], []
        for link in links:
            if not link.get("peer_id"):
                continue
            read_at = _instant(link.get("synced_at")) or at
            since = read_at - timedelta(days=30)
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT message_id,sent_at,text FROM local_messages "
                    "WHERE record_id=? AND chat_id=? AND sent_at>=? AND text!='' "
                    "ORDER BY sent_at DESC",
                    (ident, link["peer_id"], (since - timedelta(days=1)).date().isoformat()),
                )
            ]
            valid, invalid, future = [], 0, 0
            for row in rows:
                stamp = _instant(row["sent_at"])
                if stamp is None:
                    invalid += 1
                    continue
                if stamp > read_at:
                    future += 1
                    continue
                if stamp < since:
                    continue
                valid.append(
                    {
                        "text": redact(row["text"], []),
                        "date": stamp.isoformat(),
                        "url": link["url"] + "/" + row["message_id"],
                    }
                )
            key = link["url"]
            windows[key] = {"link": link, "messages": valid, "invalid": invalid, "future": future}
            selections[key] = []
            candidates.extend((key, row) for row in valid)

    def serialized(key, messages):
        return json.dumps(
            {
                "messages_stored_30d": len(windows[key]["messages"]),
                "messages_selected": len(messages),
                "selection": "Темы сроков/претензий и свежие сообщения; без разметки тональности",
                "messages": sorted(messages, key=lambda r: r["date"]),
            },
            ensure_ascii=False,
        )

    candidates.sort(key=lambda x: (bool(SIGNAL.search(x[1]["text"])), x[1]["date"]), reverse=True)
    used = 0
    for key, message in candidates:
        previous = len(serialized(key, selections[key])) if selections[key] else 0
        updated = serialized(key, [*selections[key], message])
        if used - previous + len(updated) <= MODEL_CHAR_BUDGET:
            used += len(updated) - previous
            selections[key].append(message)
    result, coverage = [], []
    for key, window in windows.items():
        link, valid, chosen = window["link"], window["messages"], selections[key]
        detail = {
            "url": key,
            "stored_30d": len(valid),
            "selected_for_facts": len(chosen),
            "excluded_invalid_timestamps": window["invalid"],
            "excluded_future": window["future"],
        }
        coverage.append(detail)
        if chosen:
            chosen.sort(key=lambda r: r["date"])
            result.append(
                {
                    "source_id": "report-chat:" + str(link["peer_id"]),
                    "url": link["url"],
                    "title": link.get("title") or "Публичный чат объекта",
                    "text": serialized(key, chosen),
                    "source_kind": "chat",
                    "contour": "communities",
                    "read_status": "loaded",
                    "published_at": None,
                    "checked_at": link.get("synced_at"),
                    "count": len(valid),
                    "messages": chosen,
                    "coverage": {
                        "read": len(valid),
                        "selected_for_facts": len(chosen),
                        "partial": link.get("status") == "partial" or len(chosen) < len(valid),
                        "labelled": False,
                    },
                }
            )
    return result, coverage


def ensure_public_chats(store, ident, *, telegram=None, research=None, at=None):
    at = _instant(at) if at is not None else datetime.now(UTC)
    if at is None:
        raise ValueError("Для чтения чатов нужна дата с часовым поясом")
    value = {
        "version": VERSION,
        "checked_at": at.isoformat(),
        "documents": [],
        "read": [],
        "skipped": [],
        "errors": [],
        "status": "not_connected",
    }
    if telegram is None or not telegram.status().get("session_saved"):
        return value
    record = store.get(ident)
    if record["dataset"] == "demo" or record.get("status") in {"rejected", "closed", "later"}:
        return {**value, "status": "skipped"}
    # Respect the existing public-chat preference; it is already on by default.
    from radar.web.operations import settings

    if not settings(store)["screen_chats"]:
        return {**value, "status": "disabled"}
    links = telegram.links(ident)
    with store.connect() as db:
        rejected = set()
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='chat_candidates'").fetchone():
            rejected = {
                r[0]
                for r in db.execute(
                    "SELECT url FROM chat_candidates WHERE record_id=? AND status='rejected'",
                    (ident,),
                )
            }
    candidates = {u: r for u, r in _candidates(record, research).items() if u not in rejected}
    accepted_links = []
    inspected = 0
    for link in links:
        if len(accepted_links) >= MAX_CHATS:
            break
        url = _public_url(link["url"])
        if not url or url != link["url"] or url in rejected:
            continue
        if not link.get("peer_id"):
            if inspected >= MAX_INSPECTIONS:
                continue
            inspected += 1
            try:
                metadata = telegram.inspect(url)
                if not metadata.get("public") or not metadata.get("peer_id"):
                    raise ValueError("Непубличный источник")
                with store.connect() as db:
                    db.execute(
                        "UPDATE telegram_links SET peer_id=? WHERE record_id=? AND url=?",
                        (str(metadata["peer_id"]), ident, url),
                    )
                link = {**link, "peer_id": str(metadata["peer_id"])}
            except (ValueError, TimeoutError, OSError):
                value["skipped"].append(
                    {"url": url, "reason": "Публичность и идентичность источника не подтверждены"}
                )
                continue
        accepted_links.append(link)
    links = accepted_links
    existing = {r["url"] for r in links}
    for url, candidate in candidates.items():
        if len(links) >= MAX_CHATS or inspected >= MAX_INSPECTIONS:
            break
        if url in existing:
            continue
        inspected += 1
        try:
            metadata = telegram.inspect(url)
            if _public_url(metadata.get("url", url)) != url or not metadata.get("peer_id"):
                raise ValueError("Неверная идентичность источника")
            accepted, reason = _match(record, metadata, candidate)
            if not accepted:
                value["skipped"].append({"url": url, "reason": reason})
                continue
            telegram.add(ident, TelegramLink(url=url))
            # Pin the inspected identity before a later username reassignment.
            with store.connect() as db:
                db.execute(
                    "UPDATE telegram_links SET peer_id=? WHERE record_id=? AND url=?",
                    (metadata["peer_id"], ident, url),
                )
            links = [r for r in telegram.links(ident) if r["url"] in {*existing, url}]
            existing.add(url)
        except (ValueError, TimeoutError, OSError):
            value["skipped"].append({"url": url, "reason": "Проверка публичного чата не завершена"})
    for link in links[:MAX_CHATS]:
        if link["url"] in rejected:
            continue
        try:
            stamp = _instant(link.get("synced_at"))
            fresh = bool(
                stamp
                and stamp.tzinfo
                and timedelta(0) <= at - stamp.astimezone(UTC) < timedelta(hours=12)
            )
            if fresh:
                value["read"].append(
                    {"url": link["url"], "cached": True, "partial": link.get("status") == "partial"}
                )
                continue
            read = telegram.sync(ident, link["url"], limit=MAX_MESSAGES, recent=True)
            value["read"].append(
                {
                    "url": link["url"],
                    "cached": False,
                    "read": read["read"],
                    "partial": read["partial"],
                }
            )
        except (ValueError, TimeoutError, OSError):
            value["errors"].append(
                {"url": link["url"], "reason": "Сообщения публичного чата не удалось обновить"}
            )
    selected_urls = {link["url"] for link in links[:MAX_CHATS]}
    current_links = [
        r
        for r in telegram.links(ident)
        if r["url"] in selected_urls
        and (stamp := _instant(r.get("synced_at"))) is not None
        and stamp <= at + timedelta(minutes=5)
    ]
    value["documents"], value["selection"] = _documents(store, ident, current_links, at.isoformat())
    for read in value["read"]:
        detail = next((d for d in value["selection"] if d["url"] == read["url"]), {})
        read.update(
            stored_30d=detail.get("stored_30d", 0),
            selected_for_facts=detail.get("selected_for_facts", 0),
        )
    partial = any(d["coverage"]["partial"] for d in value["documents"])
    if value["errors"]:
        value["status"] = "partial" if value["documents"] else "unavailable"
    elif value["documents"]:
        value["status"] = "partial" if partial else "completed"
    elif value["read"]:
        value["status"] = "no_messages"
    else:
        value["status"] = "no_matching_chats" if value["skipped"] else "no_candidates"
    return value
