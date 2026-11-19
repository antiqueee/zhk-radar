import re

import pytest

from radar.reports import matrix


def codes(shortlist):
    return {row["move"] for row in shortlist["picked"] + shortlist["reserve"]}


def test_full_library_references_and_card_contracts_are_valid():
    assert {
        key: len(matrix.MATRIX[key])
        for key in (
            "moves",
            "problems",
            "tactics",
            "antipatterns",
            "accelerators",
            "factors",
            "cases",
        )
    } == {
        "moves": 76,
        "problems": 59,
        "tactics": 41,
        "antipatterns": 22,
        "accelerators": 24,
        "factors": 5,
        "cases": 31,
    }
    assert matrix.validate() == []


def test_matching_metadata_separates_buyer_problems_from_city_and_world_case_claims():
    metadata = matrix.metadata()
    assert len(metadata["problems"]) == 54
    assert all(set(row) == {"id", "problem", "topic"} for row in metadata["problems"])
    assert len(matrix.metadata("city")["problems"]) == 5
    assert not any(key in metadata for key in ("cases", "world_note", "agency_results"))


def test_deterministic_selection_filters_scope_stage_and_limits_product_concentration():
    shortlist = matrix.suggest(2, problems=["Q02", "Q14", "Q20"], tactics=["P21", "P36"])
    assert 2 <= len(shortlist["picked"]) <= 4
    for row in shortlist["picked"] + shortlist["reserve"]:
        move = matrix.MATRIX["moves"][row["move"]]
        assert 2 in move["stages"] and "dolshiki" in move["scope"]
    for field in ("lever", "product"):
        assert all(
            sum(row[field] == value for row in shortlist["picked"]) <= 2
            for value in {row[field] for row in shortlist["picked"]}
        )
    assert matrix.suggest(2, problems=["Q02", "Q14", "Q20"], tactics=["P21", "P36"]) == shortlist
    assert not matrix.validate_move(
        2, shortlist["picked"][0]["product"], shortlist["picked"][0]["move"]
    )


def test_repeated_matches_do_not_inflate_scores_or_group_stability():
    single = matrix.suggest(2, problems=["Q02"], factors=["unity"])
    repeated = matrix.suggest(2, problems=["Q02", "Q02"], factors=["unity", "unity", "unity"])
    assert repeated == single and not repeated["outlook_rule"]
    three = matrix.suggest(2, factors=(value for value in list(matrix.MATRIX["factors"])[:3]))
    assert "не затухнет без действий" in three["outlook_rule"]


def test_monitoring_requires_a_real_accessible_community_including_fallback():
    without = matrix.suggest(2)
    assert "I01" not in codes(without)
    with_community = matrix.suggest(2, has_public_communities=True)
    assert not codes(with_community)  # A channel alone is not a problem to sell.
    problem = next(
        k for k, p in matrix.MATRIX["problems"].items() if "I01" in p["moves"] and 2 in p["stages"]
    )
    assert "I01" not in codes(matrix.suggest(2, problems=[problem]))
    assert "I01" in codes(matrix.suggest(2, problems=[problem], has_public_communities=True))


def test_diagnostic_fallback_requires_a_confirmed_communication_gap():
    assert not codes(matrix.suggest(1))
    assert not codes(matrix.suggest(1, confirmed_communication_gap="yes"))
    assert codes(matrix.suggest(1, confirmed_communication_gap=True)) == {"I04", "A06"}
    # Section 6.3 still permits an early-signal complement to a grounded tactic.
    allowed = matrix.suggest(1, tactics=["P01"], has_public_communities=True)
    assert {"D03", "I01"} <= codes(allowed)
    assert "I04" not in codes(allowed)


def test_independent_expertise_requires_commitment_to_publish_any_result():
    problem = next(
        k for k, p in matrix.MATRIX["problems"].items() if "B05" in p["moves"] and 2 in p["stages"]
    )
    pending = matrix.suggest(2, problems=[problem])
    assert "B05" not in codes(pending)
    assert any(row["move"] == "B05" and row["condition"] for row in pending["conditional"])
    confirmed = matrix.suggest(2, problems=[problem], confirmed_conditions=["B05"])
    assert "B05" in codes(confirmed)


def test_card_adaptation_preserves_product_limits_and_does_not_expose_world_cases_by_default():
    move_id = next(k for k, row in matrix.MATRIX["moves"].items() if row.get("world_note"))
    card = matrix.card(move_id)
    assert "note" not in card and "world_note" not in card
    assert (
        matrix.card(move_id, world_notes=True)["note"]
        == matrix.MATRIX["moves"][move_id]["world_note"]
    )
    assert (
        card["service"] == matrix.PRODUCTS["products"][matrix.MATRIX["moves"][move_id]["product"]]
    )
    assert matrix.card("B05")["limits"]
    card["agency"].clear()
    assert matrix.card(move_id)["agency"]
    assert matrix.validate_move(2, "несуществующий продукт", move_id)
    assert matrix.validate_move(2, "position", "invented")
    assert matrix.validate_move(2, "position", "K01")


@pytest.mark.parametrize(
    "params",
    [
        {"stage": 0},
        {"stage": 2, "scope": "city"},
        {"stage": 2, "problems": ["invented"]},
        {"stage": 2, "tactics": ["invented"]},
    ],
)
def test_untrusted_matching_output_fails_closed(params):
    with pytest.raises(ValueError):
        matrix.suggest(**params)


def test_antipattern_patterns_include_private_infiltration_and_suppression():
    patterns = matrix.antipattern_stems()
    for text in ("войти в закрытый чат под видом дольщика", "удаляем негативные отзывы"):
        assert any(re.search(pattern, text, re.I) for pattern in patterns)
