"""The final assembled page prompts must retain scoped layout and truth rules."""

import pytest

from radar.reports import prompts_v8 as prompts


@pytest.mark.parametrize("page", ["situation", "assessment", "scenario", "offer"])
def test_final_page_prompt_retains_only_its_own_compact_budget_and_shared_fact_rules(page):
    prompt = getattr(prompts, page.upper())
    assert prompts.COMPACT_PAGE_RULES[page] in prompt
    assert prompts.EDITORIAL_FACTUAL_RULES in prompt
    for other in prompts.COMPACT_PAGE_RULES:
        if other != page:
            assert prompts.COMPACT_PAGE_RULES[other] not in prompt


def test_editorial_page_revision_does_not_change_expensive_shared_extraction_instructions():
    # COMMON is used by every cached model stage, including validated Facts.
    for page_rule in prompts.COMPACT_PAGE_RULES.values():
        assert page_rule not in prompts.COMMON
        assert page_rule not in prompts.FACTS
    assert prompts.EDITORIAL_FACTUAL_RULES not in prompts.COMMON
    assert prompts.CRITIC_DISPROOF_RULES not in prompts.COMMON


def test_offer_override_and_critic_append_keep_the_actual_final_instructions():
    # Protect against the historical later OFFER assignment discarding rules.
    assert prompts.OFFER.endswith(prompts.STRATEGIC_OFFER_RULES)
    assert prompts.STRATEGIC_OFFER_RULES.endswith(prompts.EDITORIAL_FACTUAL_RULES)
    assert prompts.CRITIC.endswith(prompts.STRATEGIC_CRITIC_RULES)
    assert prompts.STRATEGIC_CRITIC_RULES.endswith(prompts.CRITIC_DISPROOF_RULES)


def test_strategic_method_is_distinct_from_a_claim_that_a_group_or_event_exists():
    rules = " ".join(prompts.EDITORIAL_FACTUAL_RULES.split())
    # One rule must contain both halves of the distinction. This prevents a
    # blanket ban on useful negotiation methods while fixing invented groups.
    assert "само по себе не доказывает" in rules
    assert "Переговоры с активом допустимы как метод" in rules
    assert "Историческое обращение не становится новым или повторным" in rules
    assert "нельзя обещать опередить уже поданную жалобу" in rules


def test_critic_explicitly_tests_support_counterevidence_and_valid_conditional_offers():
    rule = " ".join(prompts.CRITIC_DISPROOF_RULES.split())
    assert "конкретным Fact и его kind/датой/источником" in rule
    assert "сильнейший контрфакт" in rule
    assert "давняя претензия плюс свежая строительная публикация" in rule
    assert "сравнительные периоды" in rule
    assert "честный условный риск и будущее действие агентства" in rule
    assert "Не ставь failed за рабочий бюджет слов" in rule
