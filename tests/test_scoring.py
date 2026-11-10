import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from radar.domain import Evidence, House, Sales
from radar.pipeline.scoring import assess, confidence

TODAY = date(2026, 11, 10)


def signal(kind="supervision", trust="A", origin="original", age=0):
    return Evidence(
        kind=kind,
        occurred_at=TODAY - timedelta(days=age),
        trust=trust,
        origin=origin,
        quote="Точная цитата",
        document_id="doc",
    )


def test_golden_synthetic_scenario(cfg):
    data = json.loads(Path("tests/fixtures/scenario.json").read_text())
    result = assess(
        House.model_validate(data["house"]),
        [Evidence.model_validate(s) for s in data["signals"]],
        Sales.model_validate(data["sales"]),
        [],
        TODAY,
        cfg,
    )
    assert (result.severity, result.sellability, result.window) == (73, 65, 90)
    assert result.priority == "A"
    assert result.early_rules == ["R3"]


def test_reprints_do_not_create_independence_or_score(cfg):
    h = House(eisgs_id="1", label="1")
    a = signal()
    b = signal(trust="B")
    assert confidence([a, b]) == "medium"
    assert confidence([a, b.model_copy(update={"origin": "independent"})]) == "high"
    result = assess(h, [a] * 10, Sales(), [], TODAY, cfg)
    assert result.severity == 10
    assert result.sellability == 0


@pytest.mark.parametrize("age,expected", [(60, 15), (61, 0), (-1, 0)])
def test_media_freshness(cfg, age, expected):
    result = assess(
        House(eisgs_id="1", label="1"), [signal("federal_media", age=age)], Sales(), [], TODAY, cfg
    )
    assert result.severity == expected


def test_low_confidence_caps_a_and_stoplist_blocks_digest(cfg):
    h = House(
        eisgs_id="1",
        label="1",
        flats=600,
        commissioning=TODAY - timedelta(days=5),
        transfer_initial=date(2025, 1, 1),
        transfer=date(2027, 1, 1),
        transfer_shifts=2,
    )
    result = assess(
        h,
        [signal(trust="C")],
        Sales(state="active", solvent=True, buyer_known=True, stoplisted=True),
        [],
        TODAY,
        cfg,
    )
    assert result.severity >= 60
    assert result.priority == "B"
    assert result.eligible is False


@pytest.mark.parametrize("flats,expected", [(149, 8), (150, 10), (500, 10), (501, 12)])
def test_scale_boundaries(cfg, flats, expected):
    result = assess(
        House(eisgs_id="1", label="1", flats=flats), [signal()], Sales(), [], TODAY, cfg
    )
    assert result.severity == expected


def test_missing_information_does_not_become_safe_or_sellable(cfg):
    result = assess(House(eisgs_id="1", label="1"), [], Sales(), [], TODAY, cfg)
    assert (result.sellability, result.window, result.priority) == (0, 0, "none")
    assert not result.eligible
    assert len(result.limitations) == 4
