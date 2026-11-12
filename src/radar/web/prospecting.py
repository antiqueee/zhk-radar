"""Developer-first search seeds. Registry aggregates are leads, never house-level facts."""

import json
import re

from radar.web import registry, stoplist
from radar.web.dossiers import normalized
from radar.web.store import now

ERZ_REGIONS = {
    "Москва": "moskva",
    "Московская область": "moskovskaya-oblast",
    "Санкт-Петербург": "sankt-peterburg",
    "Ленинградская область": "leningradskaya-oblast",
}


def seeds(store, regions, fetcher=None):
    fetcher = fetcher or registry.fetch_erz
    registry.initialize(store)
    portfolios, errors = [], []
    for region in regions:
        slug = ERZ_REGIONS.get(region)
        if not slug:
            continue
        with store.connect() as db:
            saved = db.execute(
                "SELECT payload FROM erz_snapshots WHERE region=? AND observed_at>=? "
                "AND json_extract(payload,'$.scope_verified')=1",
                (slug, now()[:10]),
            ).fetchall()
        try:
            rows = [json.loads(row[0]) for row in saved] if saved else fetcher(store, slug)["rows"]
            for row in rows:
                portfolios.append({**row, "region": region})
        except (ValueError, OSError) as exc:
            errors.append({"stage": "registry", "detail": str(exc), "region": region})
    # Alternate regions, so Moscow's largest developers do not consume every seed slot.
    groups = [
        sorted(
            [r for r in portfolios if r["region"] == region and (r.get("delayed_area") or 0) > 0],
            key=lambda r: -(r.get("delayed_area") or 0),
        )
        for region in regions
    ]
    leads = [group[i] for i in range(6) for group in groups if i < len(group)][:8]
    queries = [
        f'"{r["developer"]}" {r["region"]} ЖК перенос срока передачи ключей отзывы'
        for r in leads
        if not stoplist.blocked(store, {"id": "", "developer": r["developer"]})
    ]
    # Agency spreadsheets supply additional names, not evidence or ready-made conclusions.
    with store.connect() as db:
        records = [
            json.loads(r[0])
            for r in db.execute("SELECT payload FROM records WHERE dataset='imported' ORDER BY id")
        ]
    eligible = [r for r in records if r.get("region") in regions and not stoplist.blocked(store, r)]
    seen = set()
    known, projects = [], []
    for region in regions:
        for r in [r for r in eligible if r["region"] == region][:2]:
            name = re.sub(r"^(жк|жилой комплекс)\s+", "", normalized(r["name"]))
            key = (name, region)
            if key in seen or len(name) < 4:
                continue
            seen.add(key)
            known.append(f'"{name}" {region} перенос сроков жалобы отзывы дольщиков')
            if r.get("granularity") != "developer":
                projects.append(r)
    return {
        "queries": list(dict.fromkeys(queries + known[:8])),
        "developers": portfolios,
        "projects": projects[:4],
        "errors": errors,
        "limitation": "ЕРЗ: первая страница рейтинга четырёх приоритетных регионов. "
        "Перенос в портфеле — основание искать ЖК, а не доказательство нарушения по дому.",
    }


def save_leads(store, projects):
    """Keep agency leads in the investigation queue even without a fresh press mention."""
    from radar.web.discovery import Finding
    from radar.web.dossiers import object_key

    ids, added = [], 0
    with store.connect() as db:
        for item in projects:
            lead = Finding(
                name=item["name"],
                developer=item["developer"],
                region=item["region"],
                building=item.get("building"),
                summary="Исходный объект для проверки",
                source_index=0,
                quote="Объект из исходных материалов агентства",
            )
            ident = "web-" + object_key(lead, "agency:" + item["id"])
            payload = {
                **item,
                "id": ident,
                "seed_record_id": item["id"],
                "summary": "Объект из материалов агентства. Проверяем актуальные сроки, отзывы "
                "и открытые чаты; текущая проблема ещё не подтверждена.",
                "verification": "research_lead",
                "urls": [],
                "claims": {},
                "assessment": None,
                "first_seen_at": now(),
                "last_seen_at": now(),
                "check": "Сверить актуальные сроки и обращения по этому ЖК.",
            }
            added += db.execute(
                "INSERT OR IGNORE INTO records(id,dataset,payload,updated_at) VALUES(?,?,?,?)",
                (ident, "web", json.dumps(payload, ensure_ascii=False), now()),
            ).rowcount
            ids.append(ident)
    return ids, added
