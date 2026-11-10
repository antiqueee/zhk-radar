from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from dateutil.relativedelta import relativedelta

from radar.domain import Assessment, Evidence, House, Sales, Snapshot
from radar.pipeline.rules import early_rules


def confidence(signals: list[Evidence]) -> str:
    a = {s.origin for s in signals if s.trust == "A"}
    b = {s.origin for s in signals if s.trust == "B"}
    bc = {s.origin for s in signals if s.trust in {"B", "C"}}
    if any(x != y for x in a for y in bc):
        return "high"
    return "medium" if a or len(b) >= 2 else "low"


def assess(
    house: House,
    signals: list[Evidence],
    sales: Sales,
    history: list[Snapshot],
    today: date,
    cfg: dict,
) -> Assessment:
    signals = [s for s in signals if s.occurred_at <= today]
    severity_cfg = cfg["severity"]
    parts = {}
    limitations = []
    if house.transfer_initial and house.transfer and house.transfer > house.transfer_initial:
        parts["transfer_delay"] = next(
            (
                points
                for months, points in severity_cfg["transfer_delays"]
                if house.transfer <= house.transfer_initial + relativedelta(months=months)
            ),
            severity_cfg["long_delay"],
        )
    if max(house.commissioning_shifts, house.transfer_shifts) >= 2:
        parts["repeat_shift"] = severity_cfg["repeat_shift"]
    if house.commissioning and house.commissioning < today and not house.rve:
        parts["overdue_no_rve"] = severity_cfg["overdue_no_rve"]
    for signal in signals:
        if (
            signal.kind.endswith("_media")
            and (today - signal.occurred_at).days > severity_cfg["media_max_age_days"]
        ):
            continue
        if signal.kind in severity_cfg["signals"]:
            parts[signal.kind] = severity_cfg["signals"][signal.kind]
    scale = severity_cfg["scale"][1]
    if house.flats is None:
        limitations.append("Квартир неизвестно: коэффициент масштаба 1.0")
    elif house.flats < severity_cfg["small_flats"]:
        scale = severity_cfg["scale"][0]
    elif house.flats > severity_cfg["large_flats"]:
        scale = severity_cfg["scale"][2]
    severity = min(
        100,
        int(
            (Decimal(str(sum(parts.values()))) * Decimal(str(scale))).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        ),
    )
    sc = cfg["sales"]
    sell = sc["unknown"]
    if sales.state == "active" and sales.solvent:
        sell = sc["active"]
    elif sales.state in {"bankrupt", "third_party", "disputed"}:
        sell = sc["difficult"]
    else:
        limitations.append("Действующий платёжеспособный заказчик не подтверждён")
    for field in ["buyer_known", "public_response", "demand", "warm_contact", "other_agency"]:
        if getattr(sales, field):
            sell += sc[field]
    if sales.other_objects >= sc["portfolio_min"]:
        sell += sc["portfolio"]
    sell = max(0, min(100, sell))
    wc = cfg["window"]
    dates = [d for d in [house.transfer, None if house.rve else house.commissioning] if d]
    windows = []
    for d in dates:
        days = (d - today).days
        key = "future" if days >= 0 else "past"
        windows.append(
            next((score for bound, score in wc[key] if abs(days) <= bound), wc[f"{key}_default"])
        )
    window = max(windows, default=0)
    if not dates:
        limitations.append("Нет подтверждённых триггерных дат")
    if signals and (today - max(s.occurred_at for s in signals)).days > wc["stale_days"]:
        window = max(0, window - wc["stale_penalty"])
    rules = early_rules(
        house, history, today, cfg["rules"], any(s.kind == "supervision" for s in signals)
    )
    certainty = confidence(signals)
    c = cfg["classes"]
    priority = "watch" if signals or parts or rules else "none"
    if severity >= c["A"][0] and sell >= c["A"][1] and window >= c["A"][2]:
        priority = "A" if certainty != "low" else "B"
    elif severity >= c["B"][0] and sell >= c["B"][1]:
        priority = "B"
    elif severity >= c["C"][0] and sell < c["C"][1]:
        priority = "C"
    elif rules and severity < c["early_severity_max"]:
        priority = "early"
    if house.readiness is None:
        limitations.append("Нет готовности: R1 и R5 отключены")
    return Assessment(
        severity=severity,
        sellability=sell,
        window=window,
        confidence=certainty,
        priority=priority,
        early_rules=rules,
        eligible=priority in {"A", "B", "early"} and not sales.stoplisted,
        contributions={**parts, "scale": scale},
        limitations=limitations,
    )
