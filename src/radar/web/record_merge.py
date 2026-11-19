"""Pool sources from explicitly confirmed cards without deleting original rows."""

import hashlib
import json
from collections import defaultdict
from uuid import uuid4

from radar.web.sources import canonical_url
from radar.web.stoplist import normalized as stoplist_key
from radar.web.store import now

_WORK_TABLES = (
    "briefs", "report_research", "registry_bindings", "telegram_links",
    "local_messages", "object_conversations", "brand_analytics_matches",
)
_TEXT_FIELDS = {"name", "region", "developer", "address", "project_locality", "project_identity"}
_LIST_FIELDS = {"aliases", "official_domains", "proof_urls"}


def _json(value):
    return json.dumps(value, ensure_ascii=False)


def _url_key(url):
    try:
        return canonical_url(url)
    except (ValueError, UnicodeError):
        return url


def _unique_urls(values):
    result = {}
    for url in values:
        if url:
            result.setdefault(_url_key(url), url)
    return list(result.values())


def _identity(value):
    values = dict(value or {})
    if values.keys() - _TEXT_FIELDS - _LIST_FIELDS:
        raise ValueError("Для проекта можно указать только подтверждённые реквизиты")
    for key, item in values.items():
        if key in _TEXT_FIELDS and (not isinstance(item, str) or not item.strip()):
            raise ValueError("Пустой или некорректный реквизит проекта: " + key)
        if key in _LIST_FIELDS and (
            not isinstance(item, (list, tuple))
            or any(not isinstance(entry, str) or not entry.strip() for entry in item)
        ):
            raise ValueError("Некорректный список реквизитов проекта: " + key)
    for url in values.get("proof_urls", ()):
        canonical_url(url)
    return values


def _tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _active_task(db, tables, ident):
    return "object_tasks" in tables and db.execute(
        "SELECT 1 FROM object_tasks WHERE record_id=? AND status IN ('queued','running') LIMIT 1",
        (ident,),
    ).fetchone() is not None


def _protected(db, tables, row):
    ident = row["id"]
    reasons = ["decision"] if (
        row["dataset"] != "web" or row["status"] != "new" or row["note"]
        or row["stage"] or row["snooze_until"] or row["refusal_reason"] or row["topics"] != "[]"
    ) else []
    if "stoplist" in tables and db.execute(
        "SELECT 1 FROM stoplist WHERE kind='record' AND normalized=?", (stoplist_key(ident),)
    ).fetchone():
        reasons.append("stoplist")
    for table in _WORK_TABLES:
        if table in tables and db.execute(
            f"SELECT 1 FROM {table} WHERE record_id=? LIMIT 1", (ident,)
        ).fetchone():
            reasons.append(table)
    if "research_runs" in tables and db.execute(
        "SELECT 1 FROM research_runs WHERE record_id=? AND "
        "json_array_length(payload,'$.answers')>0 LIMIT 1",
        (ident,),
    ).fetchone():
        reasons.append("research_runs")
    if "chat_candidates" in tables and db.execute(
        "SELECT 1 FROM chat_candidates WHERE record_id=? AND status!='suggested' LIMIT 1", (ident,)
    ).fetchone():
        reasons.append("chat_candidates")
    if _active_task(db, tables, ident):
        reasons.append("object_tasks")
    return reasons


def _signal_key(signal):
    return (
        _url_key(signal.get("source_url", "")), signal.get("quote"), signal.get("event_type"),
        signal.get("event_date"), str(signal.get("building") or "").casefold(),
    )


def _source_copies(db, tables, canonical_id, members, exclusions):
    ids = [canonical_id, *members]
    placeholders = ",".join("?" for _ in ids)
    evidence, signals = defaultdict(list), defaultdict(list)
    for row in db.execute(
        f"SELECT * FROM evidence WHERE record_id IN ({placeholders}) ORDER BY id", ids
    ):
        evidence[row["record_id"]].append(row)
    if "record_signals" in tables:
        for row in db.execute(
            f"SELECT * FROM record_signals WHERE record_id IN ({placeholders}) "
            "ORDER BY created_at,id",
            ids,
        ):
            signals[row["record_id"]].append((row, json.loads(row["payload"])))
    existing_exclusions = {row["id"] for ident in members for row, _ in signals[ident]}
    if exclusions - existing_exclusions:
        raise ValueError("Исключаемые сигналы должны принадлежать объединяемым карточкам")
    seen_signals = {_signal_key(signal) for _, signal in signals[canonical_id]}
    seen_evidence = {(_url_key(row["url"]), row["quote"]) for row in evidence[canonical_id]}
    copied_signals, copied_evidence = [], []
    for ident in members:
        excluded_quotes, accepted_quotes = set(), set()
        for row, signal in signals[ident]:
            key = _signal_key(signal)
            if row["id"] in exclusions:
                excluded_quotes.add(key[:2])
                continue
            accepted_quotes.add(key[:2])
            if key in seen_signals:
                continue
            seen_signals.add(key)
            new_id = hashlib.sha256(_json([canonical_id, row["id"]]).encode()).hexdigest()
            copied_signals.append((new_id, canonical_id, row["source_id"], _json({
                **signal, "origin_record_id": signal.get("origin_record_id", ident),
                "origin_signal_id": signal.get("origin_signal_id", row["id"]),
            }), row["created_at"]))
        for row in evidence[ident]:
            key = (_url_key(row["url"]), row["quote"])
            if key in seen_evidence or key in excluded_quotes - accepted_quotes:
                continue
            seen_evidence.add(key)
            copied_evidence.append((canonical_id, *(
                row[k] for k in ("title", "url", "quote", "source_text", "created_at")
            )))
    return copied_signals, copied_evidence


def merge_project(
    store, canonical_id, member_ids, *, identity=None, exclude_signals=(), apply=False
):
    """Plan or apply a selected merge after independent confirmation of project identity."""
    updates = _identity(identity)
    ids = list(dict.fromkeys([canonical_id, *member_ids]))
    exclusions = set(exclude_signals)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        rows = {
            row["id"]: row
            for row in db.execute(
                "SELECT * FROM records WHERE id IN (" + ",".join("?" for _ in ids) + ")", ids
            )
        }
        if rows.keys() != set(ids):
            raise ValueError("Не найдены все выбранные карточки")
        payloads = {ident: json.loads(row["payload"]) for ident, row in rows.items()}
        canonical = payloads[canonical_id]
        if canonical.get("merged_into") or rows[canonical_id]["dataset"] == "demo":
            raise ValueError("Основной должна быть активная рабочая карточка проекта")
        if canonical.get("building") or canonical.get("granularity") not in {
            None, "complex", "project"
        }:
            raise ValueError("Основная карточка должна относиться ко всему ЖК")
        tables = _tables(db)
        linked, pending, kept_separate = [], [], {}
        for ident in ids[1:]:
            parent = payloads[ident].get("merged_into")
            if parent == canonical_id:
                linked.append(ident)
            elif parent or (reasons := _protected(db, tables, rows[ident])):
                kept_separate[ident] = ["other_project"] if parent else reasons
            else:
                pending.append(ident)
        members = [*pending, *linked]
        signals, evidence = _source_copies(db, tables, canonical_id, members, exclusions)
        fields = {
            **canonical,
            **{key: value for key, value in updates.items() if key in _TEXT_FIELDS},
        }
        for key in ("aliases", "official_domains"):
            if key in updates:
                fields[key] = list(dict.fromkeys([*(canonical.get(key) or ()), *updates[key]]))
        if members:
            fields["granularity"] = "project"
            fields["source_record_ids"] = list(dict.fromkeys([
                *(canonical.get("source_record_ids") or ()), canonical_id, *pending, *linked,
            ]))
            fields["urls"] = _unique_urls([
                *updates.get("proof_urls", ()), *(canonical.get("urls") or ()),
                *(url for ident in members for url in payloads[ident].get("urls") or ()),
            ])
            if duplicates := fields.get("possible_duplicates"):
                fields["possible_duplicates"] = [
                    ident for ident in duplicates if ident not in fields["source_record_ids"]
                ]
        elif updates.get("proof_urls"):
            fields["urls"] = _unique_urls([*updates["proof_urls"], *(canonical.get("urls") or ())])
        result = {
            "canonical_id": canonical_id, "members_to_link": pending, "linked_members": linked,
            "kept_separate": kept_separate, "signals_to_copy": len(signals),
            "evidence_to_copy": len(evidence), "sources": len(fields.get("urls") or ()),
            "changed": bool(fields != canonical or pending or signals or evidence),
            "merge_id": None,
        }
        if not apply or not result["changed"]:
            return result
        if _active_task(db, tables, canonical_id):
            raise ValueError("Дождитесь завершения операции по основной карточке")
        stamp = now()
        for ident, payload in [(canonical_id, fields), *(
            (ident, {**payloads[ident], "merged_into": canonical_id}) for ident in pending
        )]:
            db.execute(
                "UPDATE records SET payload=?,revision=revision+1,updated_at=? WHERE id=?",
                (_json(payload), stamp, ident),
            )
        if signals:
            db.executemany("INSERT INTO record_signals VALUES(?,?,?,?,?)", signals)
        evidence_ids = [db.execute(
            "INSERT INTO evidence(record_id,title,url,quote,source_text,created_at) "
            "VALUES(?,?,?,?,?,?)",
            row,
        ).lastrowid for row in evidence]
        result["merge_id"] = uuid4().hex
        db.execute("""CREATE TABLE IF NOT EXISTS project_merges (
          id TEXT PRIMARY KEY, canonical_id TEXT NOT NULL, payload TEXT NOT NULL,
          created_at TEXT NOT NULL, reverted_at TEXT)""")
        db.execute("INSERT INTO project_merges VALUES(?,?,?,?,NULL)", (
            result["merge_id"], canonical_id, _json({
                "before": {ident: payloads[ident] for ident in [canonical_id, *pending]},
                "after": fields, "signals": [signal[0] for signal in signals],
                "evidence": evidence_ids,
            }), stamp,
        ))
        db.execute(
            "INSERT INTO events(record_id,action,detail,created_at) VALUES(?,?,?,?)",
            (canonical_id, "project_merge", "Объединены карточки и источники проекта", stamp),
        )
        return result


def revert_project_merge(store, merge_id):
    """Reverse one merge while the linked cards and their derived work are unchanged."""
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        tables = _tables(db)
        operation = db.execute(
            "SELECT * FROM project_merges WHERE id=?", (merge_id,)
        ).fetchone() if "project_merges" in tables else None
        if operation is None:
            raise ValueError("Объединение не найдено")
        if operation["reverted_at"]:
            return False
        canonical_id = operation["canonical_id"]
        plan = json.loads(operation["payload"])
        created_at = operation["created_at"]
        for ident, before in plan["before"].items():
            row = db.execute("SELECT payload FROM records WHERE id=?", (ident,)).fetchone()
            expected = plan["after"] if ident == canonical_id else {
                **before, "merged_into": canonical_id,
            }
            if row is None or json.loads(row["payload"]) != expected:
                raise ValueError("Карточки изменились после объединения")
            if _active_task(db, tables, ident):
                raise ValueError("Дождитесь завершения операции по связанным карточкам")
        for table, column in (
            ("briefs", "created_at"), ("report_research", "updated_at"),
            ("research_runs", "created_at"), ("project_merges", "created_at"),
        ):
            if table == "project_merges":
                newer = db.execute(
                    "SELECT 1 FROM project_merges WHERE canonical_id=? AND created_at>? "
                    "AND reverted_at IS NULL LIMIT 1", (canonical_id, created_at),
                ).fetchone()
            else:
                newer = table in tables and db.execute(
                    f"SELECT 1 FROM {table} WHERE record_id=? AND {column}>=? LIMIT 1",
                    (canonical_id, created_at),
                ).fetchone()
            if newer:
                raise ValueError("После объединения по проекту появились новые материалы")
        for table, ids in (("evidence", plan["evidence"]), ("record_signals", plan["signals"])):
            if not ids:
                continue
            removed = db.execute(
                f"DELETE FROM {table} WHERE record_id=? AND id IN ("
                + ",".join("?" for _ in ids) + ")", (canonical_id, *ids),
            ).rowcount
            if removed != len(ids):
                raise ValueError("Объединённые материалы изменились")
        stamp = now()
        for ident, before in plan["before"].items():
            db.execute(
                "UPDATE records SET payload=?,revision=revision+1,updated_at=? WHERE id=?",
                (_json(before), stamp, ident),
            )
        db.execute("UPDATE project_merges SET reverted_at=? WHERE id=?", (stamp, merge_id))
        db.execute(
            "INSERT INTO events(record_id,action,detail,created_at) VALUES(?,?,?,?)",
            (canonical_id, "project_unmerge", "Объединение карточек отменено", stamp),
        )
        return True
