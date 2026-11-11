from datetime import date, timedelta

from dateutil.relativedelta import relativedelta

from radar.domain import House, Snapshot


def early_rules(
    house: House, history: list[Snapshot], today: date, cfg: dict, supervision: bool = False
) -> list[str]:
    """Risk indicators only; callers must attach registry evidence before persisting signals."""
    found = []
    due = house.commissioning
    if house.rve:
        return found
    if due and today <= due and house.readiness is not None:
        if any(
            due <= today + relativedelta(months=m) and house.readiness < pct for m, pct in cfg["r1"]
        ):
            found.append("R1")
    if (
        due
        and house.transfer
        and house.commissioning_shifts > 0
        and house.transfer_shifts == 0
        and house.transfer < due + relativedelta(months=cfg["r2_gap_months"])
    ):
        found.append("R2")
    if house.rve_checked and due and (today - due).days >= cfg["r3_overdue_days"]:
        found.append("R3")
    shifted_deadlines = [
        d
        for d, shifts in [
            (due, house.commissioning_shifts),
            (house.transfer, house.transfer_shifts),
        ]
        if d and shifts > 0
    ]
    if any(0 <= (d - today).days <= cfg["r4_days"] for d in shifted_deadlines):
        found.append("R4")
    # No extrapolation from a younger snapshot or missing readiness.
    history = [
        s
        for s in history
        if s.readiness is not None and s.on <= today - timedelta(days=cfg["r5_history_days"])
    ]
    if (
        due
        and today <= due < today + relativedelta(months=cfg["r5_months"])
        and house.readiness is not None
        and history
    ):
        previous = max(history, key=lambda s: s.on)
        if 0 <= house.readiness - previous.readiness < cfg["r5_growth_pp"]:
            found.append("R5")
    if (
        due
        and today <= due <= today + relativedelta(months=cfg["r6_months"])
        and house.delayed_share is not None
        and house.delayed_share > cfg["r6_share"]
    ):
        found.append("R6")
    if supervision and house.commissioning_shifts == 0 and house.transfer_shifts == 0:
        found.append("R7")
    return found
