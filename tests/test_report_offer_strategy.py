"""Strategic offers need a grounded task, not an invented reason to sell an audit."""

import copy
import json

import pytest

from radar.reports import pipeline_v8 as pipeline
from radar.reports import prompts_v8 as prompts
from radar.reports.schema_v8 import Offer


def inputs(*, topic="deadlines", matched=None, evidence_count=1):
    qualification = {
        "current_stage": 2,
        "problem_registry": [
            {
                "id": "main",
                "topic": topic,
                "in_scope": True,
                "status": "active",
                "active_dated_evidence_count": evidence_count,
            }
        ],
    }
    matching = {
        "problems": [{"problem_id": "main", "matrix_id": matched}],
        "tactics": [],
        "accelerators": [],
        "factors": [],
    }
    focus = {"primary_problem": {"id": "main", "refs": [1]}}
    return matching, qualification, focus


def codes(selection):
    return {row["move"] for field in ("picked", "reserve") for row in selection[field]}


def test_effective_v8_prompts_keep_strategic_value_and_work_ownership_after_override():
    # Target the assembly bug: an earlier OFFER assignment must not silently
    # remove the reusable final strategy/ownership instructions.
    assert prompts.OFFER.endswith(prompts.STRATEGIC_OFFER_RULES)
    assert prompts.CRITIC.endswith(prompts.STRATEGIC_CRITIC_RULES)


@pytest.mark.parametrize("text,blocked", [
    ("Агентство сопровождает обращения, снижая риск перехода к стадии эскалации "
     "до декабря 2027 года.", True),
    ("Удержим конфликт на текущей стадии до конца 2027 года.", True),
    ("Выдача ключей перенесена на декабрь 2027 года. Снизим риск эскалации.", False),
    ("Подготовим план переговоров до декабря 2027 года и будем отслеживать обращения.", False),
    ("Снизим риск эскалации с помощью единого порядка ответов.", False),
])
def test_offer_checks_dated_escalation_claims_and_preserves_real_deliverables(text, blocked):
    assert pipeline.promises_to_limit_escalation_by_date(text) is blocked


@pytest.mark.parametrize("grounded", [True, False])
def test_reported_filing_heading_can_be_neutralized_only_with_a_grounded_body(grounded):
    source = {"n": 1, "contour": "reviews", "url": "https://forum.example/topic/1"}
    quote = "Собственники коллективно обратились в прокуратуру."
    facts = [{"source_n": 1, "date": "2026-02-01", "text": quote, "verbatim": quote}]
    fixture = json.loads((pipeline.ROOT / "gold/v8/nametkin-tower.json").read_text())
    offer = Offer.model_validate(fixture["offer"])
    entry = offer.why_now[1]
    original_title = "Коллективные обращения поданы"
    body = "На форуме сообщили об обращении в прокуратуру."
    entry.title = original_title
    entry.text = body if grounded else body + " Ведомство получило жалобу."
    original_text = entry.text

    issues = pipeline.ground_offer_authority(offer, facts, [source])

    assert ("offer.why_now.1.title" in {issue["path"] for issue in issues}) is not grounded
    assert entry.title == ("Сообщения об обращениях" if grounded else original_title)
    assert entry.text == original_text


def test_empty_matching_does_not_manufacture_a_diagnostic_or_call_the_author():
    with pytest.raises(pipeline.NeedsReview, match="не сопоставлена"):
        pipeline.select_matrix(*inputs(), has_public_communities=True)


@pytest.mark.parametrize("change", [{"status": "closed"}, {"in_scope": False}])
def test_stale_or_out_of_scope_communication_is_not_a_diagnostic_justification(change):
    matching, qualification, focus = inputs(topic="communication")
    qualification["problem_registry"][0].update(change)
    with pytest.raises(pipeline.NeedsReview, match="не сопоставлена"):
        pipeline.select_matrix(matching, qualification, focus, has_public_communities=False)


def test_undated_communication_and_a_secondary_gap_do_not_replace_the_main_problem():
    matching, qualification, focus = inputs(topic="communication", evidence_count=0)
    with pytest.raises(pipeline.NeedsReview, match="не сопоставлена"):
        pipeline.select_matrix(matching, qualification, focus, has_public_communities=False)
    matching, qualification, focus = inputs()
    secondary = copy.deepcopy(qualification["problem_registry"][0])
    secondary.update(id="other", topic="communication")
    qualification["problem_registry"].append(secondary)
    matching["problems"].append({"problem_id": "other", "matrix_id": "Q14"})
    with pytest.raises(pipeline.NeedsReview, match="не сопоставлена"):
        pipeline.select_matrix(matching, qualification, focus, has_public_communities=False)


def test_grounded_main_communication_gap_permits_the_documented_planning_fallback():
    selection = pipeline.select_matrix(*inputs(topic="communication"), has_public_communities=False)
    assert codes(selection) == {"I04", "A06"}


def test_null_problem_match_with_a_grounded_relevant_tactic_remains_eligible():
    matching, qualification, focus = inputs()
    matching["tactics"] = [{"code": "P36", "refs": [1]}]
    selection = pipeline.select_matrix(matching, qualification, focus, has_public_communities=True)
    assert "I01" in codes(selection) and len(codes(selection)) >= 2
    assert "I04" not in codes(selection)
    matching["tactics"][0]["refs"] = [2]  # Different problem's source cannot fill the gap.
    with pytest.raises(pipeline.NeedsReview, match="не сопоставлена"):
        pipeline.select_matrix(matching, qualification, focus, has_public_communities=True)


def test_secondary_problem_matching_does_not_change_the_primary_shortlist():
    matching, qualification, focus = inputs(matched="Q02")
    baseline = pipeline.select_matrix(matching, qualification, focus, has_public_communities=True)
    matching["problems"].append({"problem_id": "other", "matrix_id": "Q14"})
    matching["tactics"].append({"code": "P21", "refs": [2]})
    actual = pipeline.select_matrix(matching, qualification, focus, has_public_communities=True)
    assert actual == baseline
