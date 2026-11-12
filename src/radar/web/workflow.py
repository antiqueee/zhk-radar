"""Transparent candidate ranking and bounded, cached follow-up research."""

import hashlib
import json
import threading
from dataclasses import asdict
from datetime import UTC, date, datetime
from urllib.parse import urlsplit

import httpx
import yaml

from radar.domain import Evidence, House, Sales, Snapshot
from radar.pipeline.scoring import assess
from radar.web import registry, stoplist
from radar.web.configuration import configuration, model_config
from radar.web.discovery import Findings, ScanCancelled, extract, yandex
from radar.web.dossiers import dossier, normalized, persist_findings
from radar.web.operations import BudgetExceeded, reserve_call
from radar.web.sources import (
    Page,
    canonical_url,
    fetch_page,
    load_page,
    primary_text,
    research_excerpt,
)
from radar.web.store import now


def initialize(store):
    with store.connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS workflow_cache(
            key TEXT PRIMARY KEY,payload TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS candidate_scores(
            record_id TEXT PRIMARY KEY,payload TEXT,updated_at TEXT);
        CREATE TABLE IF NOT EXISTS research_runs(
            record_id TEXT,run_id TEXT,payload TEXT,created_at TEXT,
            PRIMARY KEY(record_id,run_id));
        CREATE TABLE IF NOT EXISTS chat_candidates(record_id TEXT,url TEXT,payload TEXT,status TEXT,
            PRIMARY KEY(record_id,url));
        """)


CACHE_LOCK = threading.RLock()


def latest_research(store, ident):
    """A cheap discovery pass must not erase an existing detailed dossier."""
    initialize(store)
    with store.connect() as db:
        row = db.execute(
            "SELECT payload FROM research_runs WHERE record_id=? ORDER BY "
            "CASE WHEN json_extract(payload,'$.depth')='cluster' THEN 1 ELSE 0 END, "
            "created_at DESC LIMIT 1",
            (ident,),
        ).fetchone()
    return json.loads(row[0]) if row else None


def cached(store, key, operation, run_id, service):
    with CACHE_LOCK:
        return _cached(store, key, operation, run_id, service)


def _cached(store, key, operation, run_id, service):
    initialize(store)
    digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
    with store.connect() as db:
        row = db.execute("SELECT payload FROM workflow_cache WHERE key=?", (digest,)).fetchone()
    if row:
        return json.loads(row[0]), True
    reserve_call(store, run_id, service)
    value = operation()
    with store.connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO workflow_cache VALUES(?,?,?)", (digest, json.dumps(value), now())
        )
    return value, False


def source_class(store, signal):
    path = store.root / "config/sources.yaml"
    cfg = yaml.safe_load(path.read_text()) if path.exists() else {}
    host = (urlsplit(signal["source_url"]).hostname or "").lower()
    for grade in ("A", "B"):
        if signal["source_kind"] == "page" and any(
            host == domain or host.endswith("." + domain) for domain in cfg.get(grade, [])
        ):
            return grade
    return "C"


def score(store, ident):
    initialize(store)
    record = store.get(ident)
    reg = registry.dossier(store, ident)
    research = dossier(store, ident)
    today = datetime.now(UTC).date()
    house = House(eisgs_id=ident, label=record["name"], rve_checked=False)
    evidence, history, sources = [], [], []
    if reg["binding"] and reg["binding"]["confirmed"] and reg["snapshots"]:
        first, last = reg["snapshots"][0], reg["snapshots"][-1]
        house = House(
            eisgs_id=last["house_id"],
            label=last["name"],
            flats=last["flats"],
            commissioning_initial=first["commissioning"],
            commissioning=last["commissioning"],
            transfer_initial=first["transfer"],
            transfer=last["transfer"],
            rve=last["rve"],
            rve_checked=last["rve_checked"],
            readiness=last["readiness"],
            commissioning_shifts=sum(
                c["field"] == "commissioning" and c["after"] > c["before"] for c in reg["changes"]
            ),
            transfer_shifts=sum(
                c["field"] == "transfer" and c["after"] > c["before"] for c in reg["changes"]
            ),
        )
        history = [
            Snapshot(on=s["observed_at"][:10], readiness=s["readiness"]) for s in reg["snapshots"]
        ]
        evidence.append(
            Evidence(
                kind="registry",
                occurred_at=last["observed_at"][:10],
                trust="C" if last["imported"] else "A",
                origin="registry",
                document_id=last["house_id"],
                quote=json.dumps(last),
            )
        )
    recent = 0
    seen_quotes = set()
    for signal in research["signals"]:
        stamp = signal["event_date"] or signal.get("published_at")
        grade = source_class(store, signal)
        # Fetch time is not evidence that an old event happened this week.
        event_day = date.fromisoformat(stamp[:10]) if stamp else None
        fresh = event_day and 0 <= (today - event_day).days <= 60
        sources.append(
            {
                "id": signal["id"],
                "class": grade,
                "dated": bool(stamp),
                "fresh": bool(fresh),
                "url": signal["source_url"],
            }
        )
        quote_key = hashlib.sha256(normalized(signal["quote"]).encode()).hexdigest()
        if quote_key in seen_quotes or not fresh:
            continue
        seen_quotes.add(quote_key)
        recent += 1
        kind = (
            "supervision"
            if grade == "A" and signal["event_type"] == "supervision"
            else ("regional_media" if grade == "B" else "claim")
        )
        evidence.append(
            Evidence(
                kind=kind,
                occurred_at=event_day,
                trust=grade,
                origin=quote_key,
                quote=signal["quote"],
                document_id=signal["id"],
            )
        )
    sales = Sales.model_validate({k: v for k, v in (reg["sales"] or {}).items() if k != "note"})
    excluded = bool(stoplist.blocked(store, record)) or record["status"] in {
        "rejected",
        "closed",
        "later",
    }
    sales = sales.model_copy(update={"stoplisted": excluded})
    result = assess(
        house,
        evidence,
        sales,
        history,
        today,
        yaml.safe_load((store.root / "config/scoring.yaml").read_text()),
    ).model_dump()
    # Reprints cannot be ruled out by different domains or wording alone.
    if result["confidence"] == "high":
        result["confidence"] = "medium"
        if result["priority"] == "A":
            result["priority"] = "B"
    result["limitations"].append(
        "Независимость публикаций не подтверждена; рейтинг предварительный."
    )
    if any(s["class"] == "C" for s in sources):
        result["limitations"].append(
            "Соцсети, отзывы и поисковые фрагменты не считаются подтверждением нарушения."
        )
    if not (reg["binding"] and reg["binding"]["confirmed"]):
        result["limitations"].append(
            "Корпус в реестре не подтверждён; сроки и готовность не начислены."
        )
    stale_registry = bool(
        reg["snapshots"]
        and (datetime.now(UTC) - datetime.fromisoformat(reg["snapshots"][-1]["observed_at"])).days
        > 30
    )
    if stale_registry:
        result["eligible"] = False
        result["limitations"].append("Реестровый снимок устарел: требуется обновление.")
    # Research queue is distinct from commercial eligibility; missing information stays visible.
    queue = (
        0
        if excluded
        else result["severity"]
        + min(recent, 5) * 5
        + (10 if result["early_rules"] else 0)
        + (5 if record.get("building") else 0)
    )
    result.update(
        record_id=ident,
        rank_score=queue,
        excluded=excluded,
        recent_signals=recent,
        sources=sources,
        reasons=[
            f"Свежих неповторяющихся сигналов: {recent}",
            f"Острота: {result['severity']}; коммерческий контекст: {result['sellability']}",
            "В очереди на исследование, не автоматическая рекомендация связаться",
        ],
        needs_research=not excluded and (recent > 0 or bool(result["early_rules"])),
    )
    with store.connect() as db:
        db.execute(
            "INSERT INTO candidate_scores VALUES(?,?,?) ON CONFLICT(record_id) "
            "DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",
            (ident, json.dumps(result), now()),
        )
    return result


def rank(store, ids):
    return sorted(
        (score(store, ident) for ident in dict.fromkeys(ids)),
        key=lambda s: (s["excluded"], -s["rank_score"], s["record_id"]),
    )


def query_documents(store, query, run_id, client, reader=fetch_page, max_pages=4):
    cfg = configuration(store.root)
    rows, hit = cached(
        store,
        ["followup-search-v2", now()[:10], query],
        lambda: yandex(client, cfg, query, "all"),
        run_id,
        "search",
    )
    documents = []
    # A single site's several result pages must not crowd out other voices.
    first_per_host, rest, seen_hosts = [], [], set()
    for row in rows:
        host = (urlsplit(row["url"]).hostname or "").removeprefix("www.")
        (rest if host in seen_hosts else first_per_host).append(row)
        seen_hosts.add(host)
    for row in [*first_per_host, *rest][:max_pages]:
        url = canonical_url(row["url"])
        page, _ = load_page(store, url, reader)
        full = page.status == "loaded"
        if not full:
            page = Page(url, url, "snippet", text=row["text"], title=row["title"], fetched_at=now())
            page.id = hashlib.sha256(("snippet:" + url + row["text"]).encode()).hexdigest()
            with store.connect() as db:
                db.execute(
                    "INSERT OR IGNORE INTO source_pages VALUES(?,?,?,?)",
                    (page.id, url, page.fetched_at, json.dumps(asdict(page))),
                )
        documents.append(
            {
                **row,
                "url": url,
                "text": research_excerpt(primary_text(url, page.text), 12000),
                "source_id": page.id,
                "source_kind": "page" if full else "snippet",
                "published_at": page.published_at,
            }
        )
    return documents, hit


def research(
    store,
    ident,
    run_id,
    client=None,
    reader=fetch_page,
    searcher=query_documents,
    extractor=extract,
    cancelled=lambda: False,
    depth="deep",
):
    initialize(store)
    record = store.get(ident)
    if record["dataset"] == "demo":
        return {"status": "demo", "note": "Внешнее исследование учебного объекта отключено"}
    if stoplist.blocked(store, record) or record["status"] in {"rejected", "closed", "later"}:
        raise ValueError("Объект в стоп-листе")
    if depth not in {"cluster", "deep"}:
        raise ValueError("Неизвестная глубина исследования")
    cfg, model = configuration(store.root), model_config(store.root)["extract"]
    if not all(
        cfg.get(k) for k in ["YANDEX_SEARCH_API_KEY", "YANDEX_FOLDER_ID", "OPENROUTER_API_KEY"]
    ):
        raise ValueError("Для исследования нужны ключи поиска и модели")
    name = record["name"].replace('"', " ").strip()
    scope = '"' + name + '" ' + record["region"] + " " + (record.get("building") or "")
    questions = [
        scope + " официальный сайт проектная декларация срок ввода передача ключей",
        scope + " отзывы дольщиков форум жалобы приемка недостатки",
        scope + " официальный сайт ход строительства ответ застройщика сроки",
        scope + " телеграм чат жителей соседей",
        scope + " почему перенесли сроки причина задержки объяснение застройщика",
        scope + " история переноса сроки первоначально уведомление дольщиков",
    ]
    if depth == "cluster":
        questions = questions[:2]
    owns = client is None
    client = client or httpx.Client(timeout=90)
    documents, errors, completed_questions = [], [], []
    cache_hits = 0
    try:
        for query in questions:
            if cancelled():
                raise ScanCancelled()
            try:
                rows, hit = searcher(store, query, run_id, client, reader)
            except ScanCancelled:
                raise
            except ValueError as exc:
                errors.append({"stage": "search", "detail": str(exc)})
                if isinstance(exc, BudgetExceeded):
                    break
                continue
            completed_questions.append(query)
            cache_hits += bool(hit)
            documents.extend(rows)
        documents = list({d["url"]: d for d in documents}.values())
        # Keep every query represented, while bounding input tokens for each paid pass.
        text_limit = 32000 if depth == "cluster" else 64000
        per_document = max(1, text_limit // max(1, len(documents)))
        documents = [{**d, "text": research_excerpt(d["text"], per_document)} for d in documents]
        persisted = {"signals_added": 0, "record_ids": []}
        if documents:
            try:
                result, _ = cached(
                    store,
                    ["followup-extract-v2", model, documents],
                    lambda: extractor(client, cfg, model, documents).model_dump(mode="json"),
                    run_id,
                    "model",
                )
                findings = Findings.model_validate(result)
                # Never force facts from a different location or building onto this record.
                persisted = persist_findings(store, findings.objects, documents, "web")
            except ValueError as exc:
                errors.append({"stage": "extract", "detail": str(exc)})
        from radar.web.research_answers import Answers, extract_answers, validate_answers

        answers = []
        if documents and extractor is extract and depth == "deep":
            try:
                response, _ = cached(
                    store,
                    ["research-answers-v5", model, scope, documents],
                    lambda: extract_answers(client, cfg, model, record, documents).model_dump(),
                    run_id,
                    "model",
                )
            except ValueError as exc:
                response = {"answers": []}
                errors.append({"stage": "answer", "detail": str(exc)})
            if response.get("rejected"):
                errors.append(
                    {"stage": "answer", "detail": "Некоторые ответы не прошли проверку схемы"}
                )
            # One bad quote must not discard all valid answers and collected pages.
            for answer in Answers.model_validate(response).answers:
                try:
                    answers.extend(validate_answers(Answers(answers=[answer]), record, documents))
                except ValueError as exc:
                    errors.append({"stage": "answer", "detail": str(exc)})
        value = {
            "answers": answers,
            "depth": depth,
            "status": "partial" if errors else "completed",
            "questions": questions,
            "questions_completed": len(completed_questions),
            "search_cache_hits": cache_hits,
            "text_characters": sum(len(d["text"]) for d in documents),
            "documents": [
                {
                    "url": d["url"],
                    "title": d["title"],
                    "kind": d["source_kind"],
                    "source_id": d["source_id"],
                }
                for d in documents
            ],
            "signals_added": persisted["signals_added"],
            "related_ids": persisted["record_ids"],
            "errors": errors,
            "note": "Страницы требуют сверки; контактное лицо не считается установленным.",
        }
        with store.connect() as db:
            db.execute(
                "INSERT INTO research_runs VALUES(?,?,?,?) ON CONFLICT(record_id,run_id) "
                "DO UPDATE SET payload=excluded.payload,created_at=excluded.created_at",
                (ident, run_id, json.dumps(value), now()),
            )
        return value
    finally:
        if owns:
            client.close()


def complete_scan(
    store,
    result,
    run_id,
    *,
    researcher=research,
    telegram=None,
    cancelled=lambda: False,
    progress=lambda *a: None,
    top=10,
    shortlist=8,
    deep_top=3,
):
    ids = list(result["record_ids"])
    regions = set(result.get("coverage", {}).get("regions", []))
    initial = rank(store, ids)
    from radar.web.screening import screen

    # Missing date/score is a reason to investigate, not to skip chats entirely.
    seed_ids = set(result.get("seed_ids", [])[:2])
    initialize(store)
    with store.connect() as db:
        last_researched = dict(
            db.execute(
                "SELECT record_id,MAX(created_at) FROM research_runs GROUP BY record_id"
            ).fetchall()
        )
    queue = sorted(
        [c for c in initial if not c["excluded"]],
        key=lambda c: (
            last_researched.get(c["record_id"], ""),
            c["record_id"] not in seed_ids,
            store.get(c["record_id"])["region"] in {"Не установлен", "Не указан", "Россия", ""},
        ),
    )[:top]
    chat_result = screen(
        store,
        [c["record_id"] for c in queue],
        run_id,
        telegram,
        cancelled=cancelled,
        progress=progress,
    )
    enriched, errors, checked_ids = [], [], []
    budget_stopped = False
    for row in chat_result["records"]:
        errors.extend(
            {"stage": "chats", "detail": error, "record_id": row["record_id"]}
            for error in row["errors"]
        )
    for index, candidate in enumerate(queue):
        if cancelled():
            raise ScanCancelled()
        progress(
            "research",
            85 + int(index * 7 / max(1, top)),
            f"Собираем источники по ЖК {index + 1} из {len(queue)}",
        )
        try:
            row = researcher(
                store, candidate["record_id"], run_id, cancelled=cancelled, depth="cluster"
            )
            checked_ids.append(candidate["record_id"])
            enriched.append({"record_id": candidate["record_id"], **row})
            errors.extend({**e, "record_id": candidate["record_id"]} for e in row.get("errors", []))
            related = [
                ident
                for ident in row.get("related_ids", [])
                if not regions
                or store.get(ident)["region"] in regions | {"Не установлен", "Россия", ""}
            ]
            ids = list(dict.fromkeys([*ids, *related]))
        except ValueError as exc:
            errors.append(
                {"stage": "research", "detail": str(exc), "record_id": candidate["record_id"]}
            )
            if isinstance(exc, BudgetExceeded):
                budget_stopped = True
                break
    # Deepen only candidates with evidence, then rotate the remainder on later scans.
    from radar.web.coverage import all_records, summarize
    from radar.web.operations import remaining_calls

    coverage = all_records(store)
    deep_queue = sorted(
        [
            c
            for c in rank(store, checked_ids)
            if not c["excluded"] and coverage.get(c["record_id"], {}).get("publications", 0)
        ],
        key=lambda c: (
            not coverage[c["record_id"]]["multiple"],
            -c.get("rank_score", 0),
        ),
    )[:deep_top]
    deepened = []
    for index, candidate in enumerate(deep_queue if not budget_stopped else []):
        if cancelled():
            raise ScanCancelled()
        if remaining_calls(store, run_id) < 6:
            errors.append(
                {
                    "stage": "research",
                    "detail": "Подробный разбор отложен: достигнут лимит вызовов.",
                }
            )
            break
        progress("research", 93 + index * 2, "Подробно разбираем выбранный ЖК")
        try:
            row = researcher(
                store, candidate["record_id"], run_id, cancelled=cancelled, depth="deep"
            )
            deepened.append(candidate["record_id"])
            enriched.append({"record_id": candidate["record_id"], **row})
            errors.extend({**e, "record_id": candidate["record_id"]} for e in row.get("errors", []))
            related = [
                i
                for i in row.get("related_ids", [])
                if not regions
                or store.get(i)["region"] in regions | {"Не установлен", "Россия", ""}
            ]
            ids = list(dict.fromkeys([*ids, *related]))
        except ValueError as exc:
            errors.append(
                {"stage": "research", "detail": str(exc), "record_id": candidate["record_id"]}
            )
            if isinstance(exc, BudgetExceeded):
                break
    coverage = all_records(store)
    ordered = rank(store, ids)
    selected = [c["record_id"] for c in ordered if not c["excluded"] and c["needs_research"]][
        :shortlist
    ]
    return {
        **result,
        "record_ids": ids,
        "found": len(ids),
        "signals_added": result.get("signals_added", 0)
        + sum(r.get("signals_added", 0) for r in enriched),
        "rankings": ordered,
        "shortlist": selected,
        "research": enriched,
        "object_research": {
            "checked": len(checked_ids),
            "deepened": len(deepened),
            "deferred": max(0, len([c for c in initial if not c["excluded"]]) - len(checked_ids)),
            "max_search_requests": top * 2 + deep_top * 4,
            "max_model_calls": top + deep_top * 2,
            "coverage": {i: coverage.get(i, summarize([])) for i in ids},
        },
        "chat_screening": chat_result,
        "errors": result.get("errors", []) + errors,
        "status": "partial"
        if errors or result["status"] == "partial" or chat_result["status"] == "partial"
        else "completed",
    }
