"""Replay real DOGMA atoms locally; no model, network or qualification bypass."""

import copy
import json
import re
from pathlib import Path

import pytest

from radar.reports.pipeline_v8 import FactsBatch, ground_facts
from radar.reports.v8 import RULES, check

ROOT = Path(__file__).resolve().parents[1]


def test_live_dogma_timestamp_atom_survives_with_a_proven_calendar_date():
    row = json.loads((ROOT / "tests/fixtures/dogma-format-regression.json").read_text())["chat"]
    original = copy.deepcopy(row)
    issues = []
    result = ground_facts(FactsBatch(facts=[row["fact"]]), [row["source"]], issues=issues)
    assert not issues
    assert len(result.facts) == 1
    fact = result.facts[0]
    assert fact.date == "2026-11-03"
    assert fact.source_n == 24 and fact.verbatim == row["source"]["text"]
    assert row == original


@pytest.mark.parametrize("value", [
    "1 г. 9 мес.", "1 год 9 месяцев", "2 года 3 мес.", "5 лет 1 месяц", "1год 9мес.",
    "37%", "31.12.2027", "II кв. 2027",
])
def test_metric_format_accepts_numeric_durations_and_preserves_existing_formats(value):
    assert re.fullmatch(RULES["fact_value_pattern"], value)


@pytest.mark.parametrize("value", ["Срок перенесли", "год 9 месяцев", "1 г. много месяцев"])
def test_numeric_duration_does_not_allow_word_fillers(value):
    assert not re.fullmatch(RULES["fact_value_pattern"], value)


@pytest.mark.parametrize("value,valid", [
    ("дек. 2027", True), ("Декабрь 2027", True), ("~15 млрд ₽", True), ("≈ 15,5 млн ₽", True),
    ("перенос 2027", False), ("дек. 2027, вероятно", False), ("~15 млрд ₽ по оценке", False),
])
def test_v8_metric_check_accepts_a_month_year_date_and_approximate_rubles(value, valid):
    path = ROOT / "src/radar/reports/gold/v8/nametkin-tower.json"
    report = json.loads(path.read_text())
    report["situation"]["facts"][1]["value"] = value
    assert (f"факт [1] «{value}»: нужно число или дата, а не слово" not in check(report)) == valid
