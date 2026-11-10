from radar.briefs.offers import PainWeight, load_offers, select_offers


def select(pains, **overrides):
    args = dict(
        stage=2,
        situation="сроки",
        chats_connected=True,
        low_confidence=False,
        blockers=set(),
        preview=True,
    )
    args.update(overrides)
    return select_offers(load_offers(), [PainWeight(topic=t, weight=w) for t, w in pains], **args)


def test_library_has_traceable_drafts_and_no_production_selection():
    library = load_offers()
    assert len(library.offers) == 12
    assert all(o.source_pages and not o.approved for o in library.offers)
    assert select([("разные_ответы", 10)], preview=False) == []


def test_information_vacuum_selects_specific_offer():
    assert select([("разные_ответы", 10)])[0].code == "O3"


def test_missing_chats_or_weak_evidence_prioritizes_diagnosis():
    for override in [{"chats_connected": False}, {"low_confidence": True}]:
        assert select([("разные_ответы", 10)], **override)[0].code == "O1"


def test_blocker_prevents_offer_including_diagnostic_fallback():
    assert select([("разные_ответы", 10)], blockers={"нет_уполномоченного_заказчика"}) == []


def test_supplements_cover_new_pains_and_are_bounded():
    offers = select(
        [
            ("разные_ответы", 10),
            ("массовые_претензии", 5),
            ("прокуратура", 2),
            ("негатив_в_отзывах", 1),
        ]
    )
    assert [o.code for o in offers] == ["O3", "O6", "O10"]


def test_engineering_pain_requires_post_occupancy():
    assert select([("протечки", 10)], situation="после_заселения")[0].code == "O7"
    assert select([("протечки", 10)], situation="сроки")[0].code == "O1"
