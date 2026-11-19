"""Store one shared stream of Brand Analytics publications and match existing ЖК cards."""

import hashlib
import ipaddress
import json
import re
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from itertools import batched
from urllib.parse import urlsplit

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, field_validator

from radar.web import stoplist
from radar.web.chats import redact
from radar.web.record_identity import RecordResolver, eis_url_id, identity_scope
from radar.web.report_search import _HOUSING, _words, aliases_for, candidate_relevance, research_url
from radar.web.sources import MAX_TEXT, is_access_page, primary_text
from radar.web.store import now

_HOUSING_PREFIX = re.compile(r"\b(?:zhk|zhil[a-z]* kompleks[a-z]*|novostroyk[a-z]*) $")
_SCOPE_SUFFIX = re.compile(
    r"(?i)[,\s—–-]+(?:(?:корпус|корп\.?|дом|очередь|этап)\s*[\w./-]+|"
    r"(?:[ivxlc]+|\d+(?:[- ]?[яй])?)\s+очередь)\s*$"
)
_REGION_UNKNOWN = {"", "россия", "не установлен", "не указан", "неизвестно"}
_SOCIAL_HOSTS = {"t.me", "vk.com", "ok.ru", "max.ru", "rutube.ru", "youtube.com", "dzen.ru"}
_REVIEW_HOSTS = {"2gis.ru", "irecommend.ru", "otzovik.com", "pronovostroy.ru", "novostroev.ru"}
_PROVIDER_HOSTS = {"brandanalytics.ru", "br-analytics.ru"}


class BrandMessage(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    provider_id: str | None = Field(
        default=None, max_length=250,
        validation_alias=AliasChoices("provider_id", "external_id", "id"),
        coerce_numbers_to_str=True,
    )
    url: str = Field(min_length=8, max_length=3000)
    text: str = Field(min_length=1, max_length=500_000)
    title: str = Field(default="", max_length=3000)
    published_at: str | None = Field(default=None, max_length=80)
    contour: str | None = Field(default=None, max_length=60)
    platform: str | None = Field(default=None, max_length=100)
    source_kind: str | None = Field(default=None, max_length=60)
    truncated: bool = False

    @field_validator("url")
    @classmethod
    def public_url(cls, value):
        url = research_url(value)
        host = urlsplit(url).hostname or ""
        if "." not in host or host.endswith((".local", ".localhost", ".internal")):
            raise ValueError("Нужна публичная ссылка на публикацию")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return url
        if not address.is_global or address.is_multicast:
            raise ValueError("Нужна публичная ссылка на публикацию")
        return url

    @field_validator("published_at")
    @classmethod
    def normalize_date(cls, value):
        if not value:
            return None
        try:
            if len(value) == 10:
                return date.fromisoformat(value).isoformat()
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo:
                return parsed.astimezone(UTC).isoformat()
        except ValueError:
            pass
        return None


def initialize(store):
    with store.connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS brand_analytics_messages(
          id INTEGER PRIMARY KEY, provider_id TEXT UNIQUE, url TEXT NOT NULL UNIQUE,
          title TEXT NOT NULL, text TEXT NOT NULL, published_at TEXT, contour TEXT NOT NULL,
          complete INTEGER NOT NULL, checked_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS brand_analytics_matches(
          message_id INTEGER NOT NULL REFERENCES brand_analytics_messages(id),
          record_id TEXT NOT NULL REFERENCES records(id),
          excerpt TEXT NOT NULL, score INTEGER NOT NULL,
          PRIMARY KEY(message_id, record_id));
        CREATE INDEX IF NOT EXISTS brand_analytics_matches_record
          ON brand_analytics_matches(record_id, message_id);
        """)


def _phrase(text):
    words = " ".join(_words(text))
    return words, re.compile(r"(?<!\w)" + re.escape(words) + r"(?!\w)")


def _scope(text):
    scope = identity_scope({"quote": text})
    phases = set(scope["phases"])
    phases.update(re.findall(r"\bочеред\w*\s+(?:№\s*)?(\d+)\b", text, re.I))
    return phases, set(scope["buildings"])


def _record_scope(record):
    return _scope(" ".join(str(record.get(key) or "") for key in ("name", "phase", "building"))
                  + " " + str((record.get("report_scope") or {}).get("phase") or ""))


def _stop_entries(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='stoplist'").fetchone():
        return []
    return list(db.execute("SELECT * FROM stoplist"))


def _catalogue(db):
    resolver = RecordResolver(db)
    exclusions = _stop_entries(db)
    previous = {}
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='report_research'").fetchone():
        previous = {
            ident: {"passport": {"aliases": json.loads(aliases)}}
            for ident, aliases in db.execute(
                "SELECT record_id,json_extract(payload,'$.passport.aliases') FROM report_research "
                "WHERE json_type(payload,'$.passport.aliases')='array'"
            )
        }
    index = defaultdict(dict)
    records = {}
    for ident, record in resolver.rows.items():
        if (resolver.canonical(ident) != ident or record.get("granularity") == "developer"
                or stoplist.match(record, exclusions)):
            continue
        aliases = aliases_for(record, previous.get(ident))
        phases, buildings = _record_scope(record)
        if record.get("granularity") == "building" and not buildings:
            continue
        records[ident] = (record, aliases, phases, buildings)
        for name in aliases:
            name = re.sub(r"(?i)^\s*(?:жк|жилой комплекс)\s+", "", name)
            for variant in {name, _SCOPE_SUFFIX.sub("", name)}:
                words, pattern = _phrase(variant)
                if len(words) >= 3:
                    index[words.split()[0]].setdefault(words, (pattern, set()))[1].add(ident)
    return resolver, records, index


def _anchors(record, words):
    developer = re.sub(r"(?i)^(?:гк|группа компаний|концерн|ооо|ао|сз)\s+", "",
                       str(record.get("developer") or ""))
    address = record.get("address") or (record.get("claims") or {}).get("Адрес") or ""
    values = [developer, record.get("region"), *re.split(r"[,;]", str(address))]
    score = 0
    for value in values:
        normalized = str(value or "").casefold().strip()
        anchor, pattern = _phrase(normalized)
        if normalized not in _REGION_UNKNOWN and len(anchor) >= 3 and pattern.search(words):
            score += 1
    return score


def _matches(message, catalogue):
    resolver, records, index = catalogue
    if (house := eis_url_id(message["url"])):
        resolved = resolver.resolve({}, message["url"])
        target = records.get(resolved.record_id)
        if target and house in resolver.houses(target[0]):
            return {resolved.record_id: (message["text"], 18)}
    eligible = {}
    # Test the name and its anchors in the same passage so multi-object posts do not cross-attach.
    segments = re.split(r"\n+|(?<!\bг\.)(?<=[.!?])\s+", message["text"])
    title = message["title"]
    for index_no, segment in enumerate(segments):
        passage = f"{title}\n{segment}" if index_no == 0 and title else segment
        words = " ".join(_words(passage))
        named = {" ".join(_words(value)) for value in re.findall(r'[«"]([^»"]+)[»"]', passage)}
        hits = [
            (match.start(), match.end(), alias, identifiers)
            for token in set(words.split())
            for alias, (pattern, identifiers) in index.get(token, {}).items()
            for match in pattern.finditer(words)
        ]
        phases, buildings = _scope(passage)
        for start, end, alias, identifiers in sorted(hits, key=lambda hit: -(hit[1] - hit[0])):
            if any(a <= start and b >= end and (a, b) != (start, end) for a, b, _, _ in hits):
                continue
            introduced = bool(_HOUSING_PREFIX.search(words[max(0, start - 55):start]))
            single_word = " " not in alias
            if (single_word and not (introduced or alias in named)
                    or not (introduced or _HOUSING.search(passage))):
                continue
            choices = []
            for ident in sorted(identifiers):
                record, aliases, target_phases, target_buildings = records[ident]
                if (target_phases and phases != target_phases
                        or target_buildings and buildings != target_buildings):
                    continue
                anchors = _anchors(record, words)
                if (len(identifiers) > 1 or single_word) and not anchors:
                    continue
                relevance = candidate_relevance(
                    record, {"url": message["url"], "title": "", "text": passage}, aliases
                )
                if not relevance["accepted"] and not (
                    relevance["reason"] == "missing_entity_context" and introduced
                ):
                    continue
                choices.append((anchors, len(target_phases) + len(target_buildings), ident))
            choices.sort(reverse=True)
            if not choices or len(choices) > 1 and choices[0][:2] == choices[1][:2]:
                continue
            anchors, scoped, ident = choices[0]
            excerpt, score = eligible.setdefault(ident, ([], 0))
            if segment.strip() and segment not in excerpt:
                excerpt.append(segment)
            eligible[ident] = (excerpt, max(score, 2 + 4 * anchors + 4 * scoped + introduced))
    return {ident: ("\n".join(excerpts), score) for ident, (excerpts, score) in eligible.items()
            if excerpts}


def _normalize(message):
    host = (urlsplit(message.url).hostname or "").removeprefix("www.")
    contour = message.contour
    if contour not in {"media", "reviews", "communities"}:
        contour = "reviews" if host in _REVIEW_HOSTS else (
            "communities" if host in _SOCIAL_HOSTS else "media"
        )
    text = primary_text(message.url, redact(message.text, [])).strip()
    title = redact(message.title, [])
    provider_link = any(host == domain or host.endswith("." + domain) for domain in _PROVIDER_HOSTS)
    complete = (bool(message.published_at) and not message.truncated and len(text) <= MAX_TEXT
                and not provider_link
                and (message.source_kind or "").casefold() not in {"snippet", "excerpt", "summary"}
                and not is_access_page(title, text))
    return {
        "provider_id": message.provider_id or None, "url": message.url, "title": title,
        "text": text[:MAX_TEXT], "published_at": message.published_at, "contour": contour,
        "complete": int(complete),
    }


def _lookup_payload(ident, row):
    return (
        ident,
        " ".join(_words(row["title"] + "\n" + row["text"])),
        " ".join(_words(row["url"])),
    )


def _initialize_lookup(db):
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='brand_analytics_lookup'"
    ).fetchone():
        return
    db.execute("CREATE VIRTUAL TABLE brand_analytics_lookup USING fts5(body, url)")
    db.executemany(
        "INSERT INTO brand_analytics_lookup(rowid,body,url) VALUES(?,?,?)",
        (_lookup_payload(row["id"], row) for row in db.execute(
            "SELECT id,url,title,text FROM brand_analytics_messages"
        )),
    )


def _replace_matches(db, ident, matches):
    previous = {r[0]: (r[1], r[2]) for r in db.execute(
        "SELECT record_id,excerpt,score FROM brand_analytics_matches WHERE message_id=?",
        (ident,),
    )}
    if matches == previous:
        return
    db.execute("DELETE FROM brand_analytics_matches WHERE message_id=?", (ident,))
    db.executemany("INSERT INTO brand_analytics_matches VALUES(?,?,?,?)", [
        (ident, record_id, excerpt, score)
        for record_id, (excerpt, score) in matches.items()
    ])


def ingest_messages(store, messages):
    """Upsert a transport-neutral batch atomically; replays keep publication snapshots stable."""
    initialize(store)
    counters = dict.fromkeys(
        ("received", "inserted", "updated", "unchanged", "invalid", "matched"), 0
    )
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _initialize_lookup(db)
        catalogue = _catalogue(db)
        for value in messages:
            counters["received"] += 1
            try:
                row = _normalize(BrandMessage.model_validate(value))
            except (ValidationError, ValueError, UnicodeError):
                counters["invalid"] += 1
                continue
            saved = db.execute(
                "SELECT * FROM brand_analytics_messages WHERE url=? OR provider_id=?",
                (row["url"], row["provider_id"]),
            ).fetchall()
            if len(saved) > 1 or not row["text"] or (saved and row["url"] != saved[0]["url"]):
                counters["invalid"] += 1
                continue
            if not saved:
                ident = db.execute(
                    "INSERT INTO brand_analytics_messages(provider_id,url,title,text,published_at,"
                    "contour,complete,checked_at) "
                    "VALUES(:provider_id,:url,:title,:text,:published_at,"
                    ":contour,:complete,:checked_at)",
                    {**row, "checked_at": now()},
                ).lastrowid
                db.execute(
                    "INSERT INTO brand_analytics_lookup(rowid,body,url) VALUES(?,?,?)",
                    _lookup_payload(ident, row),
                )
                counters["inserted"] += 1
            else:
                saved = saved[0]
                ident = saved["id"]
                if saved["complete"] > row["complete"]:
                    row = dict(saved)
                else:
                    row["provider_id"] = saved["provider_id"] or row["provider_id"]
                if all(saved[key] == value for key, value in row.items()):
                    counters["unchanged"] += 1
                else:
                    db.execute(
                        "UPDATE brand_analytics_messages SET provider_id=:provider_id,url=:url,"
                        "title=:title,text=:text,published_at=:published_at,contour=:contour,"
                        "complete=:complete,checked_at=:checked_at WHERE id=:id",
                        {**row, "checked_at": now(), "id": ident},
                    )
                    if row["title"] != saved["title"] or row["text"] != saved["text"]:
                        db.execute("DELETE FROM brand_analytics_lookup WHERE rowid=?", (ident,))
                        db.execute(
                            "INSERT INTO brand_analytics_lookup(rowid,body,url) VALUES(?,?,?)",
                            _lookup_payload(ident, row),
                        )
                    counters["updated"] += 1
            matches = _matches(row, catalogue)
            _replace_matches(db, ident, matches)
            counters["matched"] += bool(matches)
    return counters


def _has_messages(db):
    return bool(db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='brand_analytics_messages'"
    ).fetchone() and db.execute("SELECT 1 FROM brand_analytics_messages LIMIT 1").fetchone())


def link_saved_messages(store, record_ids):
    """Refresh relevant saved messages against the whole catalogue after identity changes."""
    targets = set(record_ids)
    if not targets:
        return
    with store.connect() as db:
        if not _has_messages(db):
            return
        db.execute("BEGIN IMMEDIATE")
        _initialize_lookup(db)
        catalogue = _catalogue(db)
        resolver, records, index = catalogue
        terms = {
            f'body : "{alias}"' for group in index.values()
            for alias, (_, identifiers) in group.items() if identifiers & targets
        }
        terms.update(
            f'url : "{house}"' for ident in targets & records.keys()
            for house in resolver.houses(records[ident][0]) if house.isdigit()
        )
        candidates = set()
        for group in batched(sorted(terms), 100):
            candidates.update(row[0] for row in db.execute(
                "SELECT rowid FROM brand_analytics_lookup WHERE brand_analytics_lookup MATCH ?",
                (" OR ".join(group),),
            ))
        for group in batched(sorted(targets), 400):
            slots = ",".join("?" for _ in group)
            candidates.update(row[0] for row in db.execute(
                f"SELECT DISTINCT message_id FROM brand_analytics_matches WHERE record_id IN "
                f"({slots})", group,
            ))
        for group in batched(sorted(candidates), 400):
            slots = ",".join("?" for _ in group)
            for row in db.execute(
                f"SELECT * FROM brand_analytics_messages WHERE id IN ({slots})", group,
            ):
                _replace_matches(db, row["id"], _matches(row, catalogue))


def _target_words(value):
    value = re.sub(
        r"^(?:жк|жилой комплекс|гк|группа компаний)\s+", "", str(value or "").strip(), flags=re.I,
    )
    return _words(value)


def search_saved_records(store, *, regions, target, days):
    """Additional existing search results from recent, locally matched publications."""
    today = datetime.now(UTC).date()
    begin = (today - timedelta(days=days - 1)).isoformat()
    wanted = _target_words(target)
    with store.connect() as db:
        if not _has_messages(db):
            return []
        _, catalogue, _ = _catalogue(db)
        rows = db.execute(
            "SELECT x.record_id,r.status,MAX(m.published_at) latest FROM brand_analytics_matches x "
            "JOIN brand_analytics_messages m ON m.id=x.message_id "
            "JOIN records r ON r.id=x.record_id "
            "WHERE m.complete=1 AND substr(m.published_at,1,10) BETWEEN ? AND ? "
            "GROUP BY x.record_id ORDER BY latest DESC,x.record_id", (begin, today.isoformat()),
        )
        found = []
        for row in rows:
            ident = row["record_id"]
            if ident not in catalogue or row["status"] == "rejected":
                continue
            record, aliases, _, _ = catalogue[ident]
            if record.get("region") not in regions:
                continue
            if wanted and not any(wanted == _target_words(value) for value in (
                *aliases, record.get("developer"),
            )):
                continue
            found.append(ident)
        return found


def search_summary(store, record_ids):
    cards, publications = set(), set()
    with store.connect() as db:
        if _has_messages(db):
            for group in batched(sorted(set(record_ids)), 400):
                slots = ",".join("?" for _ in group)
                for row in db.execute(
                    "SELECT x.record_id,m.id FROM brand_analytics_matches x "
                    "JOIN brand_analytics_messages m ON m.id=x.message_id "
                    f"WHERE x.record_id IN ({slots}) AND m.complete=1 "
                    "AND substr(m.published_at,1,10)<=date('now')", group,
                ):
                    cards.add(row[0])
                    publications.add(row[1])
    return {"records": len(cards), "publications": len(publications)}


def _publisher(url):
    parts = urlsplit(url)
    host = (parts.hostname or "").removeprefix("www.")
    if host == "t.me":
        return host + "/" + parts.path.removeprefix("/s/").lstrip("/").split("/")[0].casefold()
    if host == "vk.com" and (wall := re.match(r"/wall(-?\d+)_", parts.path)):
        return host + "/" + wall[1]
    return host


def report_documents(store, record_id, *, limit=5):
    """Only dated full-text publications may supply evidence to an object report."""
    initialize(store)
    with store.connect() as db:
        record = db.execute(
            "SELECT dataset,payload FROM records WHERE id=?", (record_id,)
        ).fetchone()
        if not record or record["dataset"] == "demo":
            return []
        payload = {**json.loads(record["payload"]), "id": record_id}
        if payload.get("merged_into") or stoplist.match(payload, _stop_entries(db)):
            return []
        rows = db.execute(
            "SELECT m.*, x.excerpt, x.score FROM brand_analytics_matches x "
            "JOIN brand_analytics_messages m ON m.id=x.message_id "
            "WHERE x.record_id=? AND m.complete=1 AND m.published_at IS NOT NULL "
            "AND substr(m.published_at,1,10)<=? ORDER BY m.published_at DESC,x.score DESC,m.id",
            (record_id, datetime.now(UTC).date().isoformat()),
        )
        documents, seen_body, seen_host = [], set(), set()
        for row in rows:
            host = _publisher(row["url"])
            body = " ".join(_words(row["excerpt"]))
            if len(documents) >= limit:
                break
            if host in seen_host or body in seen_body:
                continue
            seen_host.add(host)
            seen_body.add(body)
            source_id = hashlib.sha256(row["url"].encode()).hexdigest()
            documents.append({
                "source_id": source_id, "url": row["url"], "title": row["title"],
                "text": row["excerpt"], "published_at": row["published_at"],
                "checked_at": row["checked_at"], "contour": row["contour"],
                "source_kind": "page", "read_status": "loaded", "truncated": False,
                "origin": "brand_analytics",
            })
    return documents


def status(store):
    initialize(store)
    with store.connect() as db:
        rows = db.execute(
            "SELECT count(*) AS messages, sum(complete) AS complete, "
            "max(checked_at) AS last_imported_at FROM brand_analytics_messages"
        ).fetchone()
        linked = db.execute(
            "SELECT count(DISTINCT message_id), count(DISTINCT record_id) "
            "FROM brand_analytics_matches"
        ).fetchone()
        return {**dict(rows), "complete": rows["complete"] or 0,
                "matched_messages": linked[0], "matched_records": linked[1]}
