"""Durable optional weekly integrations; one failed source does not abort the digest."""

import json

from radar.web import extra_sources, registry, sheets, stoplist
from radar.web.store import now


def complete_steps(store, due, settings, fetcher=registry.fetch_house, syncer=sheets.sync):
    registry.initialize(store)
    with store.connect() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS weekly_steps("
            "due TEXT,name TEXT,payload TEXT,created_at TEXT,PRIMARY KEY(due,name))"
        )
        bindings = list(db.execute("SELECT * FROM registry_bindings WHERE confirmed=1"))
    tasks = []
    if settings.get("sync_registry"):
        for binding in bindings:
            record = store.get(binding["record_id"])
            if record["dataset"] == "demo" or stoplist.blocked(store, record):
                continue
            house_id = binding["house_id"]
            tasks.append(("ЕИСЖС " + house_id, lambda ident=house_id: fetcher(store, ident)))
    extra_sources.initialize(store)
    with store.connect() as db:
        bindings = list(db.execute("SELECT * FROM extra_bindings"))
    for binding in bindings:
        cfg = extra_sources.Binding.model_validate_json(binding["payload"])
        record = store.get(binding["record_id"])
        if (
            cfg.automatic
            and cfg.confirmed
            and record["dataset"] != "demo"
            and not stoplist.blocked(store, record)
        ):
            tasks.append(
                (
                    "Внешние сигналы " + record["name"],
                    lambda ident=record["id"]: extra_sources.collect(
                        store, ident, "weekly:" + due, scheduled=True
                    ),
                )
            )
    if settings.get("sync_sheets"):
        tasks.append(("Google Таблица", lambda: syncer(store)))
    results = []
    for name, task in tasks:
        with store.connect() as db:
            row = db.execute(
                "SELECT payload FROM weekly_steps WHERE due=? AND name=?", (due, name)
            ).fetchone()
        if row:
            results.append(json.loads(row[0]))
            continue
        try:
            outcome = task()
            result = {
                "name": name,
                "status": "частично; проверьте источники"
                if isinstance(outcome, dict) and outcome.get("status") == "partial"
                else "обновлено",
            }
        except Exception:
            # Errors from SDKs may contain credentials; details are in each adapter's UI.
            result = {"name": name, "status": "не обновлено; нужна проверка подключения"}
        with store.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO weekly_steps VALUES(?,?,?,?)",
                (due, name, json.dumps(result), now()),
            )
        results.append(result)
    return results
