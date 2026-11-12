"""Build an immutable report snapshot independently of HTTP and background workers."""

import hashlib
import json

from radar.briefs.offers import PainWeight, load_offers, select_offers
from radar.briefs.report import build_report
from radar.web import registry, stoplist
from radar.web.chats import aggregates
from radar.web.dossiers import dossier
from radar.web.pdf import render_pdf


def payload_for(store, ident):
    root = store.root
    record = store.get(ident)
    if record["status"] == "rejected":
        raise ValueError("Объект исключён. Сначала измените статус.")
    if stoplist.blocked(store, record):
        raise ValueError("Объект в стоп-листе. Снимите ограничение перед подготовкой предложения")
    library = load_offers(root / "config/offers.yaml")
    chat_stats = aggregates(store, ident)
    research = dossier(store, ident)
    topics = {t: 1 for t in record["topics"]}
    for topic, count in research["topics"].items():
        topics[topic] = topics.get(topic, 0) + count
    for topic, count in chat_stats["topics"]:
        if topic != "не_размечено":
            topics[topic] = topics.get(topic, 0) + count
    selected = select_offers(
        library,
        [PainWeight(topic=t, weight=w) for t, w in topics.items()],
        record["stage"],
        record["situation"],
        chats_connected=bool(chat_stats["messages_30d"]) or record["dataset"] == "demo",
        low_confidence=record["dataset"] != "demo",
        blockers=set(),
        preview=True,
    )
    payload = {
        key: record[key]
        for key in [
            "name",
            "developer",
            "region",
            "summary",
            "check",
            "dataset",
            "file",
            "sheet",
            "row",
            "assessment",
            "evidence",
        ]
    }
    payload.update(
        offers=[o.model_dump() for o in selected],
        urls=list(dict.fromkeys([*record["urls"], *[e["url"] for e in record["evidence"]]])),
        record_id=ident,
        registry=registry.dossier(store, ident),
        dossier=research,
        building=record.get("building"),
        stage=record["stage"],
        situation=record["situation"],
        topics=sorted(topics),
        chat_stats=chat_stats if chat_stats["chats"] else None,
        note=record["note"],
        library_version=library.version,
        limitation="Вымышленный учебный сценарий. Не использовать для коммерческого контакта."
        if record["dataset"] == "demo"
        else (
            "Выводы отражают утверждения источников и требуют проверки. "
            "Охват чатов ограничен добавленными источниками. Совпадение цитаты"
            " с введённым текстом "
            "не подтверждает подлинность документа. Начальное предложение — диагностика."
        ),
    )
    payload["report"] = build_report(payload, library)
    return payload


def build(store, ident):
    payload = payload_for(store, ident)
    from radar.web import cases

    base_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    matches = cases.matching(store, payload)
    with store.connect() as db:
        rows = db.execute("SELECT * FROM briefs WHERE record_id=? ORDER BY id DESC", (ident,))
        for row in rows:
            saved = json.loads(row["payload"])
            if (
                saved.get("author", {}).get("base_fingerprint") == base_hash
                and saved.get("cases", []) == matches
            ):
                return {**dict(row), "payload": saved}
    render_pdf(payload)
    return store.save_brief(ident, payload)
