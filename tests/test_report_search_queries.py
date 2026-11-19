from datetime import date

from radar.web.report_search import CONTOURS, Entity, aliases_for, query_plan, second_pass_plan


def test_contour_queries_use_one_intent_and_keep_all_aliases_within_budget():
    record = {"name": "ЖК Светлый мир «Станция Л»", "region": "Санкт-Петербург"}
    aliases = aliases_for(record)
    assert aliases[-1] == "Станция Л"
    plan = query_plan(record, aliases, date(2026, 11, 16))
    assert len(plan) <= 24
    assert {row["contour"] for row in plan if row["window"] == "12m"} == set(CONTOURS)
    assert {row["contour"] for row in plan if row["window"] == "30d"} == set(CONTOURS)
    assert all(any(f'"{alias}"' in row["query"] for row in plan) for alias in aliases)
    assert not any("дольщики перенос сроков коллективное" in row["query"] for row in plan)


def test_second_pass_searches_new_alias_independently_and_keeps_event_query_short():
    record = {"name": "ЖК Новый квартал", "region": "Москва"}
    entities = [
        Entity(text="Старое название", source_url="https://example.org", kind="alias"),
        Entity(
            text="Палаточный лагерь дольщиков у здания районной администрации",
            source_url="https://example.org",
            kind="event",
        ),
    ]
    plan = second_pass_plan(record, [record["name"], "Новый квартал"], entities)
    alias_query = next(row["query"] for row in plan if "Старое название" in row["query"])
    assert "Новый квартал" not in alias_query
    assert any(row["query"].endswith('"палаточный лагерь"') for row in plan)
    assert len(plan) <= 16


def test_krost_queries_pin_house_id_and_target_second_phase_without_losing_aliases():
    record = {
        "name": "Новая Звезда — II очередь",
        "region": "Москва",
        "developer": "КРОСТ",
        "address": "Коммунарка, ул. Бачуринская",
        "aliases": ["Новая Звезда 2", "Новая Звезда II очередь", "Новая Звезда"],
        "official_domains": ["krost.ru"],
        "report_scope": {"house_id": "66399"},
    }
    aliases = aliases_for(record)
    first = query_plan(record, aliases, date(2026, 11, 18))
    second = second_pass_plan(record, aliases, [], today=date(2026, 11, 18))
    assert len(first) <= 24 and len(second) <= 16
    assert len(first) + len(second) <= 40
    assert {row["contour"] for row in first if row["window"] == "30d"} == set(CONTOURS)
    assert all(any(f'"{alias}"' in row["query"] for row in first) for alias in aliases)
    assert any(row["query"] == 'site:наш.дом.рф "66399"' for row in first)
    assert any(row["query"].startswith('site:krost.ru "Новая Звезда II очередь"') for row in first)
    assert any('"Бачуринская"' in row["query"] and row["contour"] == "official" for row in first)
    assert any(row["contour"] == "media" and "Бачуринская" not in row["query"] for row in first)


def test_dogma_uses_official_domain_and_federal_sites_instead_of_moscow_local_sites():
    record = {
        "name": "Догма Парк",
        "region": "Краснодарский край, Краснодар",
        "aliases": ["DOGMA PARK"],
        "official_domains": ["dogma.ru"],
    }
    first = query_plan(record, aliases_for(record), date(2026, 11, 18))
    sites = {row["query"].split()[0] for row in first if row["query"].startswith("site:")}
    assert {"site:dogma.ru", "site:epp.genproc.gov.ru", "site:realty.ria.ru"} <= sites
    assert not sites & {"site:mos.ru", "site:stroi.mos.ru", "site:msk1.ru", "site:fontanka.ru"}
    assert any('"DOGMA PARK"' in row["query"] for row in first)


def test_nagatino_prefers_verified_seed_host_and_cyrillic_alias_to_the_weak_final_alias():
    record = {
        "name": "Nagatino I-Land",
        "region": "Москва",
        "aliases": ["Нагатино Айленд", "Нагатино Ай-Лэнд", "Нагатино Ленд"],
        "official_domains": [
            "наш.дом.рф", "nagatino-iland.ru", "etalongroup.ru", "etalongroup.com",
        ],
        "urls": ["https://наш.дом.рф/", "https://etalongroup.ru/msk/object/nagatino-i-land"],
        "report_scope": {"phase": "Этап 1.7"},
    }
    first = query_plan(record, aliases_for(record), date(2026, 11, 18))
    assert any(row["query"].startswith('site:etalongroup.ru "Нагатино Айленд"') for row in first)
    assert any(row["query"].startswith("site:mos.ru ") for row in first)
    assert any('"Этап 1.7"' in row["query"] and row["contour"] == "official" for row in first)
    assert any('"Нагатино Ленд"' in row["query"] for row in first)
    assert all('"Нагатино Айленд"' in row["query"] for row in first if row["window"] == "30d")
