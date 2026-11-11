"""Supplementary routes: registry, approvals, sheet projection and maintenance."""

import json
from datetime import UTC, datetime

from fastapi import Request, UploadFile

from radar.web import maintenance, registry, review, sheets


def install(app, store):
    @app.get("/api/records/{ident}/registry")
    def registry_dossier(ident: str):
        return registry.dossier(store, ident)

    @app.put("/api/records/{ident}/registry")
    def registry_bind(ident: str, data: registry.RegistryBinding):
        return registry.bind(store, ident, data)

    @app.delete("/api/records/{ident}/registry")
    def registry_unbind(ident: str):
        registry.initialize(store)
        with store.connect() as db:
            db.execute("DELETE FROM registry_bindings WHERE record_id=?", (ident,))
        return registry.dossier(store, ident)

    @app.post("/api/records/{ident}/registry/refresh")
    def registry_refresh(ident: str):
        binding = registry.dossier(store, ident)["binding"]
        if not binding:
            raise ValueError("Сначала укажите ID корпуса в ЕИСЖС")
        registry.fetch_house(store, binding["house_id"])
        return registry.dossier(store, ident)

    @app.put("/api/records/{ident}/sales")
    def sales_update(ident: str, data: registry.SalesUpdate):
        return registry.save_sales(store, ident, data)

    @app.post("/api/registry/import")
    def registry_import(data: registry.RegistrySnapshot):
        # Client cannot turn a manual upload into an automatically obtained source.
        return registry.save_snapshot(store, data.model_copy(update={"imported": True}))

    @app.get("/api/registry/example")
    def registry_example():
        return {
            "house_id": "12345",
            "name": "Замените: ЖК и корпус",
            "address": "",
            "developer": "",
            "commissioning": None,
            "transfer": None,
            "readiness": None,
            "rve": None,
            "rve_checked": False,
            "flats": None,
            "observed_at": datetime.now(UTC).isoformat(),
            "source_url": "https://наш.дом.рф",
            "imported": True,
        }

    @app.post("/api/registry/erz/{region}")
    def erz_refresh(region: str):
        return registry.fetch_erz(store, region)

    @app.get("/api/registry/erz")
    def erz_history():
        registry.initialize(store)
        with store.connect() as db:
            return [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT payload FROM erz_snapshots ORDER BY observed_at DESC LIMIT 100"
                )
            ]

    @app.get("/api/records/{ident}/brief-history")
    def brief_history(ident: str):
        store.get(ident)
        with store.connect() as db:
            rows = list(
                db.execute(
                    "SELECT id,version,created_at FROM briefs "
                    "WHERE record_id=? ORDER BY id DESC LIMIT 20",
                    (ident,),
                )
            )
        return [{**dict(r), "reviews": review.history(store, r["id"])} for r in rows]

    @app.get("/api/briefs/{ident}/review")
    def review_history(ident: int):
        return review.history(store, ident)

    @app.post("/api/briefs/{ident}/review")
    def review_run(ident: int):
        return review.semantic_review(store, ident)

    @app.put("/api/briefs/{ident}/approval")
    def review_approve(ident: int, data: review.OperatorReview, request: Request):
        return review.approve(store, ident, data, request.state.identity["username"])

    @app.get("/api/sheets")
    def sheets_status():
        return {
            **sheets.status(store),
            "preview": {k: max(0, len(v) - 1) for k, v in sheets.tables(store).items()},
        }

    @app.post("/api/sheets/credentials")
    async def sheets_credentials(file: UploadFile):
        raw = await file.read(30001)
        if len(raw) > 30000:
            raise ValueError("Максимальный размер JSON ключа — 30 КБ")
        try:
            return sheets.save_credentials(store.root, raw.decode("utf-8"))
        except UnicodeError:
            raise ValueError("Нужен JSON в UTF-8") from None

    @app.post("/api/sheets/sync")
    def sheets_sync():
        return sheets.sync(store)

    @app.get("/api/maintenance")
    def maintenance_status():
        return {"backups": maintenance.history(store), "error": app.state.maintenance.last_error}

    @app.post("/api/maintenance/backup")
    def maintenance_backup():
        return maintenance.backup(store)
