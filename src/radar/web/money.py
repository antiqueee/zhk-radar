"""Conservative USD reservations. Unknown/failed calls retain their full reserve."""

import contextvars
import math
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

PENDING = contextvars.ContextVar("radar_paid_call", default=None)


class MoneySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    monthly_usd: float = Field(default=200, gt=0, le=100000, allow_inf_nan=False)
    search_reserve_usd: float = Field(default=0.10, gt=0, le=100, allow_inf_nan=False)
    model_reserve_usd: float = Field(default=2, gt=0, le=100, allow_inf_nan=False)


def initialize(store):
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS money_settings(id INTEGER PRIMARY KEY,payload TEXT)")
        db.execute(
            "INSERT OR IGNORE INTO money_settings VALUES(1,?)", (MoneySettings().model_dump_json(),)
        )
        columns = {r[1] for r in db.execute("PRAGMA table_info(api_calls)")}
        if columns and "reserved_usd" not in columns:
            db.execute("ALTER TABLE api_calls ADD COLUMN reserved_usd REAL")
            db.execute("ALTER TABLE api_calls ADD COLUMN billed_usd REAL")


def settings(store):
    initialize(store)
    with store.connect() as db:
        return MoneySettings.model_validate_json(
            db.execute("SELECT payload FROM money_settings WHERE id=1").fetchone()[0]
        )


def save(store, value):
    initialize(store)
    with store.connect() as db:
        db.execute("UPDATE money_settings SET payload=? WHERE id=1", (value.model_dump_json(),))
    return summary(store)


def reserve(db, service, stamp):
    cfg = MoneySettings.model_validate_json(
        db.execute("SELECT payload FROM money_settings WHERE id=1").fetchone()[0]
    )
    amount = (
        cfg.model_reserve_usd
        if ("model" in service or "extract" in service or "review" in service)
        else cfg.search_reserve_usd
    )
    if service in {"source-hh", "source-vk"}:
        amount = 0  # Read-only public APIs; quantity limits still apply.
    spent = db.execute(
        "SELECT COALESCE(sum(COALESCE(billed_usd,reserved_usd)),0) "
        "FROM api_calls WHERE created_at>=?",
        (stamp[:7],),
    ).fetchone()[0]
    unknown = db.execute(
        "SELECT count(*) FROM api_calls WHERE created_at>=? "
        "AND reserved_usd IS NULL AND billed_usd IS NULL",
        (stamp[:7],),
    ).fetchone()[0]
    if cfg.enabled and unknown:
        from radar.web.operations import BudgetExceeded

        raise BudgetExceeded(
            "Есть старые вызовы без денежной оценки. Установите резерв для прошлого учёта."
        )
    if cfg.enabled and spent + amount > cfg.monthly_usd + 1e-8:
        from radar.web.operations import BudgetExceeded

        raise BudgetExceeded("Месячный резерв бюджета исчерпан. Новые платные вызовы остановлены.")
    return amount


def settle_response(response):
    pending = PENDING.get()
    PENDING.set(None)
    if pending is None:
        return
    store, ident = pending
    cost = response.get("usage", {}).get("cost") if isinstance(response, dict) else None
    if (
        not isinstance(cost, (int, float))
        or isinstance(cost, bool)
        or not math.isfinite(cost)
        or cost < 0
    ):
        return
    with store.connect() as db:
        db.execute("UPDATE api_calls SET billed_usd=? WHERE id=?", (cost, ident))


def summary(store):
    cfg = settings(store)
    with store.connect() as db:
        has = db.execute("SELECT 1 FROM sqlite_master WHERE name='api_calls'").fetchone()
        row = (
            db.execute(
                "SELECT COALESCE(sum(billed_usd),0), "
                "COALESCE(sum(CASE WHEN billed_usd IS NULL THEN reserved_usd ELSE 0 END),0), "
                "count(CASE WHEN billed_usd IS NULL AND reserved_usd IS NULL THEN 1 END) "
                "FROM api_calls WHERE created_at>=?",
                (datetime.now(UTC).isoformat()[:7],),
            ).fetchone()
            if has
            else (0, 0, 0)
        )
    return {
        "settings": cfg.model_dump(),
        "billed_usd": round(row[0], 6),
        "pending_reserved_usd": round(row[1], 6),
        "unpriced_calls": row[2],
        "note": "Резервы задаёт администратор. Это не гарантия счёта провайдера: "
        "тариф и фактическая стоимость могут отличаться; включите лимит также у провайдера.",
    }
