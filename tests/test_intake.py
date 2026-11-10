from radar.intake import candidates_from_rows, registry_hints


def row(number, values):
    return {
        "row": number,
        "cells": [
            {"coordinate": f"{col}{number}", "value": value, "formula": None, "url": None}
            for col, value in zip("ABCDEFGHIJKLMNO", values, strict=False)
        ],
    }


def test_source_rank_is_not_radar_rank_and_id_not_guessed_from_building():
    rows = [
        row(1, ["Девелопер", "ЖК", "Корпус / ID ЕИСЖС", "Приоритет проверки"]),
        row(2, ["DOGMA", "Самолёт", "43.1", "A"]),
    ]
    result = candidates_from_rows("test.xlsx", "abc", "Реестр", rows)[0]
    assert result.developer == "DOGMA"
    assert result.subject == "Самолёт"
    assert result.claims["Приоритет проверки"] == "A"
    assert not result.eligible_for_digest
    assert result.eisgs_id_hints == []
    assert result.verification == "unverified_import"


def test_registry_id_hints_only_from_explicit_marker_or_registry():
    assert registry_hints(
        [
            "Дом ID 46127",
            "ЕИСЖС №59921",
            "корпус 12345",
            "https://example.org/news/99999",
            "https://наш.дом.рф/проверка-новостроек/жк-тест-66399/",
        ]
    ) == ["46127", "59921", "66399"]


def test_reimport_id_is_stable_and_cell_provenance_preserved():
    rows = [row(4, ["Группа", "Объект / корпус"]), row(5, ["Застройщик", "Корпуса 1–3"])]
    a = candidates_from_rows("test.xlsx", "abc", "ЖК", rows)
    b = candidates_from_rows("test.xlsx", "abc", "ЖК", rows)
    assert a == b
    assert a[0].cells["B5"] == "Корпуса 1–3"
    assert a[0].granularity == "mixed"
    assert not candidates_from_rows("test.xlsx", "abc", "ЖК", [row(1, ["Непонятная таблица"])])
