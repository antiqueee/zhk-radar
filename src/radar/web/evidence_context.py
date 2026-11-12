"""Current-object evidence shared by the author, dialogue and method retrieval."""

from radar.briefs.report import text_topics
from radar.web.chats import redact


def sources_for(payload):
    sources = {}
    for signal in payload.get("dossier", {}).get("signals", []):
        ident = signal["source_id"]
        if ident not in sources:
            sources[ident] = {
                "id": ident,
                "text": signal["quote"],
                "topics": list(signal["topics"]),
                "kind": signal["source_kind"],
                "url": signal["source_url"],
                "published_at": signal.get("published_at"),
                "event_date": signal.get("event_date"),
            }
        else:
            source = sources[ident]
            if signal["quote"] not in source["text"]:
                source["text"] += "\n" + signal["quote"]
            source["topics"] = sorted(set(source["topics"]) | set(signal["topics"]))
    for e in payload.get("evidence", []):
        ident = "evidence:" + str(e["id"])
        sources[ident] = {
            "id": ident,
            "text": e["quote"],
            "topics": payload["topics"],
            "kind": "operator_supplied",
            "url": e["url"],
        }
    for answer in (payload.get("followup") or {}).get("answers", []):
        ident = answer["source_id"]
        if ident in sources:
            if answer["quote"] not in sources[ident]["text"]:
                sources[ident]["text"] += "\n" + answer["quote"]
        else:
            sources[ident] = {
                "id": ident,
                "text": answer["quote"],
                "topics": [],
                "kind": answer["kind"],
                "url": answer["url"],
            }
    for i, e in enumerate((payload.get("chat_stats") or {}).get("examples", [])):
        ident = "chat:" + str(i)
        sources[ident] = {
            "id": ident,
            "text": redact(e["text"], []),
            "topics": [e["topic"]],
            "kind": "chat",
            "url": e.get("url"),
        }
    for source in sources.values():
        source["topics"] = sorted(set(source["topics"]) | text_topics(source["text"]))
    return list(sources.values())
