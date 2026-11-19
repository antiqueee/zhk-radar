import pytest

from radar.reports.pipeline_v8 import ResearchFacts
from radar.reports.source_copy import (
    attribute_media_stage_sign,
    attribute_published_handover,
    attribute_secondary_source,
    forum_numbers_supported,
    missing_deadline_publishers,
)


@pytest.mark.parametrize("serialized", [False, True])
def test_joint_media_sign_keeps_each_outlets_grounded_role(serialized):
    shift = "Застройщик перенёс срок выдачи ключей с марта 2026 на декабрь 2027."
    comment = "Мы столкнулись с проблемами грунта и провели дополнительные исследования."
    facts = ResearchFacts.model_validate({"facts": [
        {"source_n": 1, "kind": "fact", "text": shift, "verbatim": shift,
         "date": "2026-04-02", "who_says": "Новостное издание"},
        {"source_n": 2, "kind": "fact", "text": "Перенос срока связан с проблемами грунта.",
         "verbatim": comment, "date": None, "who_says": "Директор застройщика"},
    ]})
    if serialized:
        facts = facts.model_dump()
    sources = [
        {"n": 1, "url": "https://report.example/one", "contour": "media", "text": shift},
        {"n": 2, "url": "https://www.paper.example/two", "contour": "media", "text": comment},
    ]
    sign = ("Публикации report.example и paper.example зафиксировали перенос срока "
            "и реакцию дольщиков в апреле–мае 2026 г.")
    assert attribute_media_stage_sign(sign, facts, sources, [2, 1]) == (
        "report.example сообщил о переносе срока выдачи ключей. "
        "paper.example привела объяснение застройщика."
    )
    assert attribute_media_stage_sign(sign, facts, sources, [1]) == sign
    assert attribute_media_stage_sign(sign, facts, sources, []) == sign
    for changes in ({"text": "Текст отсутствует."}, {"kind": "snippet"}, {"contour": "reviews"}):
        limited = [sources[0], {**sources[1], **changes}]
        assert attribute_media_stage_sign(sign, facts, limited, [1, 2]) == sign
    other_claim = sign + " Редакция запросила комментарий."
    assert attribute_media_stage_sign(other_claim, facts, sources, [1, 2]) == other_claim


def test_joint_media_sign_does_not_treat_a_reporter_paraphrase_as_direct_explanation():
    shift = "Застройщик перенёс срок выдачи ключей."
    paraphrase = "Перенос срока строительная компания объясняет проблемами грунта."
    facts = {"facts": [
        {"source_n": 1, "kind": "fact", "verbatim": shift},
        {"source_n": 2, "kind": "fact", "text": paraphrase, "verbatim": paraphrase,
         "who_says": "Застройщик"},
    ]}
    sources = [
        {"n": 1, "url": "https://report.example/one", "contour": "media", "text": shift},
        {"n": 2, "url": "https://paper.example/two", "contour": "media", "text": paraphrase},
    ]
    sign = ("Публикации report.example и paper.example зафиксировали перенос срока "
            "и реакцию дольщиков в апреле–мае 2026 г.")
    assert attribute_media_stage_sign(sign, facts, sources, [1, 2]) == sign


def test_reported_key_shift_uses_outlet_when_only_its_publication_day_is_known():
    proof = "Застройщик «Эталон» перенёс срок выдачи ключей с марта 2026 на декабрь 2027."
    facts = {"facts": [{"source_n": 1, "verbatim": proof}]}
    sources = [{"n": 1, "url": "https://rnrf.ru/news/item", "contour": "media",
                "published_at": "2026-01-30"}]
    shift = "«Эталон» объявил о переносе выдачи ключей с марта 2026 на декабрь 2027."
    assert attribute_published_handover(shift, facts, sources, [1], event_date="2026-01-30") == (
        "rnrf.ru сообщил о переносе выдачи ключей с марта 2026 на декабрь 2027."
    )
    dated = "30 января 2026 года «Эталон» сдвинул выдачу ключей с марта 2026 на декабрь 2027."
    assert attribute_published_handover(dated, facts, sources) == (
        "30 января 2026 года rnrf.ru сообщил о переносе выдачи ключей с марта 2026 на декабрь 2027."
    )
    passive = "30 января 2026 года выдача ключей сдвинута с марта 2026 на декабрь 2027."
    assert attribute_published_handover(passive, facts, sources) == (
        "30 января 2026 года rnrf.ru сообщил о переносе выдачи ключей с марта 2026 на декабрь 2027."
    )
    explicit = (
        "30 января 2026 года застройщик «Эталон» перенёс выдачу ключей в ЖК Nagatino I-Land "
        "с марта 2026 на декабрь 2027 года, сославшись на проблемы с грунтом. "
        "На 28 октября 2026 года готовность объекта составила 37%."
    )
    attributed = attribute_published_handover(explicit, facts, sources)
    assert attributed == explicit.replace("застройщик", "rnrf.ru сообщил, что застройщик", 1)
    assert attribute_published_handover(attributed, facts, sources) == attributed
    assert attribute_published_handover(
        shift, facts, sources, [1], event_date="2026-01-29",
    ) == shift
    facts["facts"][0]["verbatim"] = "30 января 2026 года " + proof
    assert attribute_published_handover(dated, facts, sources) == dated


@pytest.mark.parametrize("serialized", [False, True])
def test_named_registry_and_published_deadline_keep_their_own_sources(serialized):
    texts = [
        "Согласно данным на наш.дом.рф, ключи перенесли с марта 2026 на 31.12.2027 "
        "на 1 год 9 месяцев.",
        "Компания назвала причиной переноса непредвиденные геологические условия.",
    ]
    facts = ResearchFacts.model_validate({"facts": [
        {"source_n": n, "kind": "fact", "text": text, "verbatim": text, "date": None,
         "who_says": "Издание"}
        for n, text in enumerate(texts, 1)
    ]})
    if serialized:
        facts = facts.model_dump()
    sources = [
        {"n": 1, "url": "https://vc.ru/project"},
        {"n": 2, "url": "https://www.fontanka.ru/project"},
        {"n": 3, "url": "https://наш.дом.рф/project"},
    ]
    label = "Общий перенос срока по данным наш.дом.рф"
    assert attribute_secondary_source(label, facts, sources, [1]) == (
        "Общий перенос срока по vc.ru со ссылкой на наш.дом.рф"
    )
    assert attribute_secondary_source(label, facts, sources, [1, 3]) == label
    assert attribute_secondary_source(label, facts, sources, [2]) == label
    assert missing_deadline_publishers(
        "30.01.2026 vc.ru и 5 февраля fontanka.ru сообщили о сдвиге ключей на декабрь 2027.",
        facts, sources,
    ) == ["fontanka.ru"]
    assert missing_deadline_publishers(
        "С января 2026 vc.ru и fontanka.ru опубликовали материалы о сдвиге ключей "
        "с марта 2026 на декабрь 2027.", facts, sources,
    ) == ["fontanka.ru"]
    assert missing_deadline_publishers(
        "fontanka.ru сообщила, что выдача ключей перенесена на декабрь 2027.", facts, sources,
    ) == ["fontanka.ru"]
    assert missing_deadline_publishers(
        "vc.ru сообщил о переносе ключей на декабрь 2027. fontanka.ru привела объяснение компании.",
        facts, sources,
    ) == []


def test_forum_attribution_cannot_borrow_a_deadline_from_a_press_citation():
    facts = {"facts": [
        {"source_n": 1, "date": "2026-02-01", "text": "Собственники сообщили об обращениях.",
         "verbatim": "Собственники сообщили об обращениях."},
        {"source_n": 2, "text": "Срок переноса 1 год 9 месяцев, до декабря 2027.",
         "verbatim": "Срок переноса 1 год 9 месяцев, до декабря 2027."},
    ]}
    sources = [
        {"n": 1, "url": "https://example.net/project", "contour": "reviews"},
        {"n": 2, "url": "https://example.org/project", "contour": "media"},
    ]
    assert not forum_numbers_supported(
        "На форуме example.net дольщики обсуждают перенос на 21 месяц до декабря 2027.",
        facts, sources, [1, 2],
    )
    assert not forum_numbers_supported(
        "На форуме обсуждают перенос на декабрь 2027.", facts, sources, [1, 2],
    )
    assert forum_numbers_supported(
        "Срок перенесли на декабрь 2027. В феврале 2026 на форуме сообщили об обращениях.",
        facts, sources, [1, 2],
    )
