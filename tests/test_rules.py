from datetime import date, timedelta

import pytest

from radar.domain import House, Snapshot
from radar.pipeline.rules import early_rules

TODAY = date(2026, 11, 10)


def house(**kwargs):
    return House(eisgs_id="test", label="Корпус 1", **kwargs)


@pytest.mark.parametrize(
    "due,readiness,expected",
    [
        (date(2027, 5, 10), 69.9, True),
        (date(2027, 5, 10), 70, False),
        (date(2027, 2, 10), 84.9, True),
        (date(2027, 2, 10), 85, False),
        (date(2027, 5, 11), 10, False),
        (date(2026, 11, 9), 10, False),
        (date(2027, 2, 10), None, False),
    ],
)
def test_r1_calendar_boundaries(cfg, due, readiness, expected):
    result = early_rules(house(commissioning=due, readiness=readiness), [], TODAY, cfg["rules"])
    assert ("R1" in result) is expected


@pytest.mark.parametrize("days,expected", [(29, False), (30, True), (31, True)])
def test_r3_boundary(cfg, days, expected):
    result = early_rules(house(commissioning=TODAY - timedelta(days=days)), [], TODAY, cfg["rules"])
    assert ("R3" in result) is expected


def test_rve_stops_construction_risks(cfg):
    h = house(commissioning=date(2026, 1, 1), rve=TODAY, readiness=20)
    assert early_rules(h, [], TODAY, cfg["rules"], supervision=True) == []


@pytest.mark.parametrize(
    "age,growth,expected", [(59, 1, False), (60, 2.9, True), (60, 3, False), (60, -1, False)]
)
def test_r5_requires_sufficient_history(cfg, age, growth, expected):
    h = house(commissioning=date(2027, 1, 1), readiness=70)
    history = [Snapshot(on=TODAY - timedelta(days=age), readiness=70 - growth)]
    assert ("R5" in early_rules(h, history, TODAY, cfg["rules"])) is expected


def test_r2_r4_r6_and_missing_readiness(cfg):
    h = house(
        commissioning=date(2027, 1, 1),
        transfer=date(2027, 3, 1),
        commissioning_shifts=1,
        delayed_share=0.51,
    )
    assert early_rules(h, [], TODAY, cfg["rules"]) == ["R2", "R4", "R6"]
    assert "R6" not in early_rules(
        h.model_copy(update={"delayed_share": 0.5}), [], TODAY, cfg["rules"]
    )


def test_r7_requires_no_shifts(cfg):
    assert early_rules(house(), [], TODAY, cfg["rules"], True) == ["R7"]
    assert early_rules(house(transfer_shifts=1), [], TODAY, cfg["rules"], True) == []
