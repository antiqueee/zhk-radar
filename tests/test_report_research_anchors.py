"""Research references are identities, never an LLM guess between two coordinates."""

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from radar.reports import research_anchors as anchors
from radar.reports.author_output import AUTHOR_TOOL, parse_author_tool
from radar.reports.pipeline_v8 import ResearchFacts
from radar.web.report_evidence import normalize_research, qualify

ROOT = Path(__file__).resolve().parents[1]


def native_payload(research, facts):
    """Encode existing truthful fixtures in the native boundary contract for mocks."""
    result = copy.deepcopy(research)
    registry = anchors.fact_registry(facts)
    key_by_index = {row["index"]: key for key, row in registry.items()}
    by_source = {}
    for key, row in registry.items():
        by_source.setdefault(row["fact"]["source_n"], []).append(key)
    passport = result["passport"]
    for prefix in ("developer", "status", "developer_status", "addressee"):
        indexes = passport.pop(prefix + "_fact_indexes", [])
        refs = passport.pop(prefix + "_refs")
        passport[prefix + "_fact_keys"] = (
            [key_by_index[i] for i in indexes]
            if indexes
            else [by_source[n][0] for n in refs if n in by_source]
        )
    for phase in passport["phases"]:
        phase["fact_keys"] = [by_source[n][0] for n in phase.pop("refs") if n in by_source]
    for field in ("evidence", "stage_signs"):
        for item in result[field]:
            indexes = item.pop("fact_indexes")
            item.pop("source_n")
            item["anchor_fact_key"] = key_by_index[indexes[0]]
            item["supporting_fact_keys"] = [key_by_index[i] for i in indexes[1:]]
    return result


@pytest.fixture
def materials():
    return json.loads((ROOT / "tests/fixtures/report-research-v5.json").read_text())


def test_native_contract_uses_one_copyable_key_and_one_shared_enum(materials):
    contract = anchors.native_contract(materials["facts"])
    schema = contract.model_json_schema()
    keys = list(anchors.fact_registry(materials["facts"]))
    assert schema["$defs"]["FactKey"]["enum"] == keys
    for model in ("AnchoredEvidence", "AnchoredStageSign"):
        props = schema["$defs"][model]["properties"]
        assert props["anchor_fact_key"] == {"$ref": "#/$defs/FactKey"}
        assert not {"source_n", "fact_indexes", "anchor_fact_index"} & props.keys()
    assert all(
        not name.endswith("_refs") for name in schema["$defs"]["AnchoredPassport"]["properties"]
    )
    assert json.dumps(schema).count(keys[0]) == 1


def test_grouped_context_has_one_copy_and_keys_change_when_fact_or_order_changes(materials):
    facts = materials["facts"]
    context = anchors.anchored_context(
        {"facts": ["old"], "report_scope": {"house_id": "66399"}}, facts
    )
    groups = context["facts_by_source"]
    assert "facts" not in context and context["report_scope"]["house_id"] == "66399"
    assert sum(len(group["facts"]) for group in groups) == len(facts)
    for group in groups:
        assert all(row["source_n"] == group["source_n"] for row in group["facts"])
    first = next(iter(anchors.fact_registry(facts)))
    changed = copy.deepcopy(facts)
    changed[0]["text"] += " — другое утверждение"
    assert first not in anchors.fact_registry(changed)
    changed = copy.deepcopy(facts)
    changed = list(reversed(changed))
    assert first not in anchors.fact_registry(changed)


def test_legitimate_passport_stages_and_full_qualification_survive_unchanged(materials):
    research = normalize_research(
        materials["research"], {}, materials["sources"], materials["facts"], "2026-11-16"
    ).model_dump()
    native = native_payload(research, materials["facts"])
    # Native serialization is also parsed by the exact outgoing enum contract.
    parsed = anchors.native_contract(materials["facts"]).model_validate(native)
    issues = []
    restored = anchors.to_research(parsed, materials["facts"], issues=issues)
    restored = normalize_research(
        restored, {}, materials["sources"], materials["facts"], "2026-11-16"
    )
    assert not issues
    assert restored.model_dump() == research
    assert (
        qualify(restored, materials["sources"], materials["facts"], "2026-11-16")["verdict"]
        == "report_full"
    )


@pytest.mark.parametrize(
    "bad_key", ["f0001-not-the-right-hash", "f9999-000000000000", "", None, -1, True]
)
def test_invalid_anchor_is_discarded_never_replaced_by_another_fact(materials, bad_key):
    native = native_payload(materials["research"], materials["facts"])
    original = copy.deepcopy(native["evidence"][0])
    native["evidence"][0]["anchor_fact_key"] = bad_key
    # A valid supporting key must never replace a missing/wrong anchor.
    native["evidence"][0]["supporting_fact_keys"] = [original["anchor_fact_key"]]
    issues = []
    restored = anchors.to_research(native, materials["facts"], issues=issues)
    assert original["id"] not in {item.id for item in restored.evidence}
    assert any(i["path"].startswith("evidence/0/anchor_fact_key") for i in issues)
    assert len(restored.evidence) == len(native["evidence"]) - 1


def test_valid_key_with_foreign_source_field_is_rejected_not_silently_overwritten(materials):
    native = native_payload(materials["research"], materials["facts"])
    native["evidence"][0]["source_n"] = 99
    issues = []
    result = anchors.to_research(native, materials["facts"], issues=issues)
    assert len(result.evidence) == len(native["evidence"]) - 1
    assert issues[0]["path"] == "evidence/0/source_n"


def test_bad_supporting_key_does_not_block_valid_anchor_and_stays_in_audit(materials):
    native = native_payload(materials["research"], materials["facts"])
    native["evidence"][0]["supporting_fact_keys"].append("f0001-wrong-hash")
    issues = []
    result = anchors.to_research(native, materials["facts"], issues=issues)
    assert len(result.evidence) == len(native["evidence"])
    assert issues[0]["record"] == "f0001-wrong-hash"
    assert "supporting_fact_keys" in issues[0]["path"]


def test_single_anchor_does_not_require_an_empty_supporting_array(materials):
    native = native_payload(materials["research"], materials["facts"])
    for item in [*native["evidence"], *native["stage_signs"]]:
        item.pop("supporting_fact_keys")
    issues = []
    result = anchors.to_research(native, materials["facts"], issues=issues)
    assert not issues
    assert all(len(row.fact_indexes) == 1 for row in [*result.evidence, *result.stage_signs])


def test_stringified_stage_signs_are_resolved_with_the_same_validation(materials):
    native = native_payload(materials["research"], materials["facts"])
    bad_sign = {**native["stage_signs"][0], "anchor_fact_key": "f9999-wrong"}
    native["stage_signs"].append(bad_sign)
    expected_issues = []
    expected = anchors.to_research(native, materials["facts"], issues=expected_issues)

    encoded = {**native, "stage_signs": json.dumps(native["stage_signs"], ensure_ascii=False)}
    call = {"type": "function", "function": {"name": AUTHOR_TOOL, "arguments": json.dumps(encoded)}}
    response = {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [call]}}]}
    parsed = parse_author_tool(response, anchors.RawAnchoredResearch, repair_arrays=True)
    assert parsed.stage_signs == native["stage_signs"]
    issues = []
    restored = anchors.to_research(parsed, materials["facts"], issues=issues)
    assert restored == expected
    assert issues == expected_issues
    assert issues[-1]["path"] == f"stage_signs/{len(native['stage_signs']) - 1}/anchor_fact_key"


@pytest.mark.parametrize("value", ["[", "{}", "null", json.dumps([None] * 21)])
def test_invalid_stringified_stage_signs_are_rejected(materials, value):
    native = native_payload(materials["research"], materials["facts"])
    arguments = json.dumps({**native, "stage_signs": value})
    call = {"type": "function", "function": {"name": AUTHOR_TOOL, "arguments": arguments}}
    response = {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [call]}}]}
    with pytest.raises(ValidationError) as error:
        parse_author_tool(response, anchors.RawAnchoredResearch, repair_arrays=True)
    assert error.value.errors()[0]["loc"] == ("stage_signs",)


def test_malformed_operator_checks_preserve_anchored_research(materials):
    native = native_payload(materials["research"], materials["facts"])
    expected = anchors.to_research(native, materials["facts"], issues=[])
    native["manual_checks"] = '["Уточнить адрес в "ЕИСЖС".", "Сверить паспорт."]'
    call = {"type": "function", "function": {"name": AUTHOR_TOOL, "arguments": json.dumps(native)}}
    response = {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [call]}}]}
    raw = parse_author_tool(response, anchors.RawAnchoredResearch, repair_arrays=True)
    issues = []
    result = anchors.to_research(raw, materials["facts"], issues=issues)

    assert not issues
    assert result.model_dump(exclude={"manual_checks"}) == expected.model_dump(
        exclude={"manual_checks"}
    )
    assert result.manual_checks == [
        "Уточнить список ручных проверок. Модель вернула его в повреждённом формате."
    ]


def test_valid_stringified_operator_checks_preserve_the_original_values(materials):
    native = native_payload(materials["research"], materials["facts"])
    checks = ["Сверить паспорт", "Уточнить адрес"]
    native["manual_checks"] = json.dumps(checks, ensure_ascii=False)

    assert anchors.RawAnchoredResearch.model_validate(native).manual_checks == checks


@pytest.mark.parametrize("value", ["Сверить паспорт", "{}", "[42]", json.dumps(["Проверка"] * 16)])
def test_operator_checks_still_require_bounded_string_lists(materials, value):
    native = native_payload(materials["research"], materials["facts"])
    native["manual_checks"] = value
    with pytest.raises(ValidationError) as error:
        anchors.RawAnchoredResearch.model_validate(native)
    assert error.value.errors()[0]["loc"][0] == "manual_checks"


def test_live_248_mixed_references_cannot_escape_the_anchor_source(materials):
    fixture = json.loads((ROOT / "tests/fixtures/research-anchor-248.json").read_text())
    facts = [
        {
            "source_n": 1,
            "text": f"Проверенный факт {i}",
            "date": None,
            "who_says": "источник",
            "kind": "fact",
            "verbatim": f"Проверенный факт {i}",
        }
        for i in range(50)
    ]
    for row in fixture["facts"]:
        facts[row["index"]] = row["fact"]
    facts = ResearchFacts(facts=facts)
    native = native_payload(materials["research"], materials["facts"])
    native["evidence"], native["stage_signs"] = [], []
    key_by_index = {row["index"]: key for key, row in anchors.fact_registry(facts).items()}
    for item in fixture["rejected_links"]:
        item = copy.deepcopy(item)
        indexes = item.pop("fact_indexes")
        item.pop("source_n")
        item["anchor_fact_key"] = key_by_index[indexes[0]]
        item["supporting_fact_keys"] = [key_by_index[i] for i in indexes[1:]]
        native["stage_signs" if "stage" in item else "evidence"].append(item)
    # The unrelated old passport keys are invalid and cannot generate proof.
    issues = []
    result = anchors.to_research(native, facts, issues=issues)
    assert [(e.source_n, e.fact_indexes) for e in result.evidence] == [
        (3, [2]),
        (22, [49]),
        (13, [22, 23]),
    ]
    assert [(s.source_n, s.fact_indexes) for s in result.stage_signs] == [(3, [2])]
    assert sum("другому источнику" in issue["reason"] for issue in issues) == 2
    # This derives from the chosen Fact, never from the source number of the old model response.
    assert all(
        facts.facts[i].source_n == row.source_n
        for row in [*result.evidence, *result.stage_signs]
        for i in row.fact_indexes
    )


def test_native_unknown_enum_key_does_not_become_an_index(materials):
    native = native_payload(materials["research"], materials["facts"])
    native["evidence"][0]["anchor_fact_key"] = "f9999-wrong"
    with pytest.raises(ValidationError):
        anchors.native_contract(materials["facts"]).model_validate(native)
    # Parsing uses the raw envelope so an invalid independent row doesn't spend a paid repair.
    assert anchors.RawAnchoredResearch.model_validate(native)


def test_unknown_passport_key_cannot_fall_back_to_an_unrelated_source(materials):
    native = native_payload(materials["research"], materials["facts"])
    native["passport"]["status_fact_keys"] = ["f0001-wrong-hash"]
    issues = []
    restored = anchors.to_research(native, materials["facts"], issues=issues)
    restored = normalize_research(
        restored, {}, materials["sources"], materials["facts"], "2026-11-16"
    )
    assert restored.passport.status == "unknown"
    assert restored.passport.status_refs == restored.passport.status_fact_indexes == []
    assert (
        qualify(restored, materials["sources"], materials["facts"], "2026-11-16")["verdict"]
        == "hold"
    )
    assert issues[0]["path"] == "passport/status_fact_keys/0"


def test_invalid_stage_anchor_does_not_become_a_supported_stage(materials):
    native = native_payload(materials["research"], materials["facts"])
    item = native["stage_signs"][0]
    item["supporting_fact_keys"] = [item["anchor_fact_key"]]
    item["anchor_fact_key"] = "f9999-wrong"
    issues = []
    restored = anchors.to_research(native, materials["facts"], issues=issues)
    assert len(restored.stage_signs) == len(native["stage_signs"]) - 1
    assert issues[-1]["path"] == "stage_signs/0/anchor_fact_key"
