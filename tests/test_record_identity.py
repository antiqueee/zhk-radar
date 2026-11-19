import shutil
import sqlite3

import pytest

from radar.web.discovery import Finding
from radar.web.dossiers import dossier, persist_findings
from radar.web.prospecting import save_leads
from radar.web.record_identity import RecordResolver, identity_scope, project_name
from radar.web.record_merge import merge_project
from radar.web.registry import RegistryBinding, bind
from radar.web.store import Store


@pytest.fixture
def store(tmp_path):
    shutil.copytree("config", tmp_path / "config")
    return Store(tmp_path)


def seed(store, ident="canonical", name="Новая Звезда — II очередь", **extra):
    region = extra.pop("region", "Москва")
    developer = extra.pop("developer", "КРОСТ")
    store.ingest({"candidates": [{
        "id": ident, "subject": name, "developer": developer,
        "claims": {"Регион": region}, "urls": [], "file": "offline.xlsx",
        "sheet": "Объекты", "row": 1,
        "aliases": ["Новая Звезда II очередь", "Новая Звезда 2 очередь"],
        **extra,
    }]})
    return ident


def finding(store, name, url="https://fixture.test/article", region="Москва", **extra):
    building = " " + extra["building"] if extra.get("building") else ""
    quote = f"{region}. ЖК {name}{building}: дольщики сообщают о переносе передачи ключей."
    value = Finding(name=name, developer=extra.pop("developer", "КРОСТ"), region=region,
                    summary=quote, quote=quote, source_index=0, **extra)
    return persist_findings(store, [value], [{
        "url": url, "text": quote, "title": name, "source_kind": "page", "source_id": url,
    }])


def test_two_explicit_phase_aliases_share_canonical_and_replay_adds_nothing(store):
    ident = seed(store, eisgs_id_hints=["66399"])
    with store.connect() as db:
        db.execute("UPDATE records SET status='inwork',note='Не менять',revision=4 WHERE id=?",
                   (ident,))
    first = finding(store, "Новая Звезда II очередь")
    again = finding(store, "Новая Звезда II очередь")
    other = finding(store, "Новая Звезда 2 очередь", "https://fixture.test/other")
    assert first["record_ids"] == again["record_ids"] == other["record_ids"] == [ident]
    assert first["added"] == again["added"] == other["added"] == 0
    assert first["signals_added"] == other["signals_added"] == 1
    assert again["signals_added"] == 0
    row = store.get(ident)
    assert row["dataset"] == "imported" and row["status"] == "inwork"
    assert row["note"] == "Не менять" and row["name"] == "Новая Звезда — II очередь"
    assert row["revision"] == 6 and len(row["evidence"]) == 2
    assert not store.records("web")


def test_confirmed_house_and_exact_eis_url_resolve_a_new_name(store):
    ident = seed(store)
    bind(store, ident, RegistryBinding(house_id="66399", confirmed=True))
    result = finding(store, "Другое документированное имя",
                     "https://наш.дом.рф/сервисы/каталог-новостроек/объект/66399")
    assert result["record_ids"] == [ident] and result["added"] == 0


def test_conflicting_house_ids_do_not_merge_even_with_same_legacy_key(store):
    name = "Новая Звезда II очередь"
    first = finding(store, name, "https://наш.дом.рф/сервисы/api/kn/object/66399")
    other = finding(store, name, "https://наш.дом.рф/сервисы/api/kn/object/66400")
    again = finding(store, name, "https://наш.дом.рф/сервисы/api/kn/object/66400")
    assert first["record_ids"] != other["record_ids"]
    assert other["added"] == 1
    assert store.get(other["record_ids"][0])["possible_duplicates"] == first["record_ids"]
    assert again["record_ids"] == other["record_ids"] and again["added"] == 0
    assert len(store.records("web")) == 2


def test_old_merged_house_url_cannot_override_confirmed_canonical_house(store):
    ident = seed(store)
    bind(store, ident, RegistryBinding(house_id="66399", confirmed=True))
    url = "https://наш.дом.рф/сервисы/api/kn/object/66400"
    seed(store, "old", "Прежнее имя", aliases=[], urls=[url], merged_into=ident)
    result = finding(store, "Прежнее имя", url)
    assert result["added"] == 1 and result["record_ids"] != [ident]
    assert store.get(result["record_ids"][0])["possible_duplicates"] == [ident]


def test_same_name_in_different_regions_is_separate(store):
    seed(store, name="Учебный квартал", aliases=["Учебный квартал"])
    other = finding(store, "Учебный квартал", region="Санкт-Петербург")
    assert other["added"] == 1
    assert not store.get(other["record_ids"][0]).get("possible_duplicates")


def test_ambiguous_generic_name_does_not_pick_one_physical_house(store):
    seed(store, "phase-1", "Новая Звезда I очередь", aliases=["Новая Звезда"],
         eisgs_id_hints=["66398"])
    seed(store, "phase-2", aliases=["Новая Звезда"], eisgs_id_hints=["66399"])
    result = finding(store, "Новая Звезда")
    repeated = finding(store, "Новая Звезда")
    assert result["added"] == 1
    assert repeated["added"] == repeated["signals_added"] == 0
    lead = store.get(result["record_ids"][0])
    assert lead["possible_duplicates"] == ["phase-1", "phase-2"]
    assert len(store.records("web")) == 1


def test_generic_name_with_one_scoped_alias_still_needs_a_phase(store):
    seed(store, aliases=["Новая Звезда"], eisgs_id_hints=["66399"])
    result = finding(store, "Новая Звезда")
    assert result["added"] == 1 and result["record_ids"] != ["canonical"]


def test_conflicting_developer_does_not_merge(store):
    seed(store, aliases=["Новая Звезда II очередь"])
    other = finding(store, "Новая Звезда II очередь", developer="Другой застройщик")
    assert other["added"] == 1 and other["record_ids"] != ["canonical"]


def test_conflicting_building_does_not_merge(store):
    seed(store, aliases=["Новая Звезда II очередь"], building="корпус 1")
    other = finding(store, "Новая Звезда II очередь", building="корпус 2")
    assert other["added"] == 1 and other["record_ids"] != ["canonical"]


def test_ordinary_prose_about_a_house_is_not_a_building_identifier(store):
    ident = seed(store, name="Учебный квартал", aliases=["Учебный квартал"])
    text = "Москва. ЖК Учебный квартал: дом фактически не строится, дом не сдан."
    result = persist_findings(store, [Finding(
        name="Учебный квартал", developer="КРОСТ", region="Москва",
        summary=text, quote=text, source_index=0,
    )], [{
        "url": "https://fixture.test/prose", "text": text, "title": "Учебный квартал",
        "source_kind": "page", "source_id": "prose",
    }])
    assert result["added"] == 0 and result["record_ids"] == [ident]


def test_merged_old_name_redirects_new_independent_signals(store):
    ident = seed(store)
    seed(store, "old", "Прежнее имя объекта", aliases=[], merged_into=ident)
    result = finding(store, "Прежнее имя объекта")
    other = finding(store, "Прежнее имя объекта", "https://fixture.test/independent")
    assert result["record_ids"] == other["record_ids"] == [ident]
    assert result["added"] == other["added"] == 0
    assert len(store.get(ident)["evidence"]) == 2
    assert store.get("old")["merged_into"] == ident


def test_lead_from_existing_or_merged_import_keeps_canonical_dataset_and_edits(store):
    ident = seed(store)
    seed(store, "old", "Прежнее имя", aliases=[], merged_into=ident)
    ids, added = save_leads(store, [store.get(ident), store.get("old")])
    assert ids == [ident] and added == 0
    assert store.get(ident)["dataset"] == "imported" and store.get("old")["merged_into"] == ident
    assert not store.records("web")


def test_failed_ingestion_transaction_loses_no_existing_state_or_partial_signal(store):
    ident = seed(store)
    with store.connect() as db:
        before = dict(db.execute("SELECT * FROM records WHERE id=?", (ident,)).fetchone())
        db.execute("CREATE TRIGGER reject_event BEFORE INSERT ON events "
                   "BEGIN SELECT RAISE(ABORT,'offline forced failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="offline forced failure"):
        finding(store, "Новая Звезда II очередь")
    with store.connect() as db:
        after = dict(db.execute("SELECT * FROM records WHERE id=?", (ident,)).fetchone())
        assert before == after
        assert db.execute("SELECT count(*) FROM record_signals").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM evidence").fetchone()[0] == 0
    assert not store.records("web")


def test_unknown_region_exact_replay_retains_existing_identity(store):
    result = finding(store, "Необычный квартал", region="Не установлен")
    repeated = finding(store, "Необычный квартал", region="Не установлен")
    assert repeated["record_ids"] == result["record_ids"]
    assert repeated["added"] == repeated["signals_added"] == 0


def test_decimal_building_keeps_its_number_when_reading_old_scope(store):
    name = "Учебный парк"
    first = seed(store, name=name, building="корпус 3.1", aliases=[],
                 identity_scope={"buildings": ["3", "3.1"], "phases": []})
    assert identity_scope(store.get(first))["buildings"] == ["3.1"]
    assert finding(store, name, building="корпус 3.1")["record_ids"] == [first]
    other = finding(store, name, "https://fixture.test/other", building="корпус 3.2")
    assert other["added"] == 1 and other["record_ids"] != [first]


def test_known_project_collects_corpora_without_claiming_their_official_houses(store):
    house = "https://наш.дом.рф/сервисы/каталог-новостроек/объект/66399"
    ident = seed(store, name="ЖК «Учебный парк»", developer="ГК КРОСТ", granularity="project",
                 aliases=[], urls=[house])
    child = {
        "name": "Учебный Парк · корп. 3.1", "developer": "КРОСТ", "region": "Москва",
        "building": "корпус № 3.1",
    }
    assert project_name(child["name"]) == "учебный парк"
    assert identity_scope(child)["buildings"] == ["3.1"]
    with store.connect() as db:
        resolver = RecordResolver(db)
        assert resolver.resolve_project(child).record_id == ident
        assert resolver.resolve(child, house).record_id is None
        for changes in ({"region": "Санкт-Петербург"}, {"developer": "Другой"}):
            assert resolver.resolve_project({**child, **changes}).record_id is None


def test_broad_region_needs_a_project_anchor_or_a_known_locality(store):
    ident = seed(store, name="Учебный парк", region="Московская область",
                 project_identity="verified-project-a", aliases=[])
    child = {
        "name": "Учебный парк", "developer": "КРОСТ", "region": "Московская область",
    }
    with store.connect() as db:
        resolver = RecordResolver(db)
        ambiguous = resolver.resolve_project(child)
        assert ambiguous.record_id is None and ambiguous.candidates == (ident,)
        verified = resolver.resolve_project({
            **child,
            "name": "Подтверждённое другое название",
            "project_identity": "verified-project-a",
        })
        assert (verified.record_id, verified.reason) == (ident, "verified_project")
        wrong = resolver.resolve_project({**child, "project_identity": "verified-project-b"})
        assert wrong.record_id is None and not wrong.candidates


def test_project_resolver_requires_an_unambiguous_project_level_parent(store):
    name = "Учебный парк"
    seed(store, "parent-1", name=name, aliases=[])
    seed(store, "parent-2", name=name, aliases=[])
    seed(store, "bound", name=name, aliases=[])
    bind(store, "bound", RegistryBinding(house_id="66399", confirmed=True))
    seed(store, "letter-scope", name=name + " · корпус А", aliases=[])
    child = {"name": name, "developer": "КРОСТ", "region": "Москва", "building": "корпус 3.1"}
    with store.connect() as db:
        resolver = RecordResolver(db)
        result = resolver.resolve_project(child)
        assert result.record_id is None and result.candidates == ("parent-1", "parent-2")
        assert resolver.resolve_project(child, exclude=("parent-2",)).record_id == "parent-1"


def corpus_publication(store, *, region, url, quote, building, header=""):
    value = Finding(
        name="ЖК Учебный парк", developer="ГК КРОСТ", region=region, building=building,
        summary="Зафиксировано сообщение дольщиков о сроках", quote=quote, source_index=0,
    )
    return persist_findings(store, [value], [{
        "url": url, "text": header + quote, "title": "Публикации об объектах застройщиков",
        "source_kind": "page", "source_id": url,
    }])


def test_new_corpora_share_a_proven_project_while_preserving_each_signal(store):
    url = "https://fixture.test/first-two"
    quote = "В Москве ГК КРОСТ строит ЖК Учебный парк: корпус № 3.1 и корпус 3.2."
    first, second = [corpus_publication(
        store, region="Москва", url=url, quote=quote, building=building,
    ) for building in ("корпус № 3.1", "корпус 3.2")]
    assert first["record_ids"] == second["record_ids"]
    assert first["added"] == first["signals_added"] == second["signals_added"] == 1
    assert second["added"] == 0
    ident = first["record_ids"][0]
    card = store.get(ident)
    assert card["granularity"] == "project" and card["building"] is None
    assert card["identity_scope"] == {"phases": [], "buildings": []}
    assert {signal["building"] for signal in dossier(store, ident)["signals"]} == {
        "корпус № 3.1", "корпус 3.2",
    }
    assert len(card["evidence"]) == 1


@pytest.mark.parametrize("excluded", [False, True])
def test_exact_member_publication_replay_respects_project_merge_provenance(store, excluded):
    scope = "корпус 3.1"
    quote = f"ГК КРОСТ: дольщики ЖК Учебный парк, {scope}, ждут передачи ключей."
    url = "https://fixture.test/developer-reviews"
    initial = corpus_publication(
        store, region="Новосибирск", url=url, quote=quote, building=scope,
        header="Автор из России, Новосибирск. ",
    )
    member = initial["record_ids"][0]
    assert store.get(member)["granularity"] == "building"
    parent = seed(store, "parent", name="Учебный парк", region="Москва",
                  granularity="project", aliases=[])
    signal = dossier(store, member)["signals"][0]
    merge_project(store, parent, [member], exclude_signals=[signal["id"]] if excluded else (),
                  apply=True)
    replay = corpus_publication(
        store, region="Россия", url=url, quote=quote, building=scope,
        header="Автор из России, Новосибирск. ",
    )
    assert replay["record_ids"] == ([] if excluded else [parent])
    assert replay["added"] == replay["signals_added"] == 0
    assert replay["discarded"] == int(excluded)
    assert len(dossier(store, parent)["signals"]) == int(not excluded)
