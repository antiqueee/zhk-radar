import pytest

from radar.reports.fact_numbers import canonical_fact_date, numeric_values, validate_fact_numbers


def check(text, date=None, *, source_text="", **metadata):
    return validate_fact_numbers(
        {"text": text, "date": date, "source_n": 1},
        {"n": 1, "text": source_text, **metadata},
    )


@pytest.mark.parametrize(
    "source,canonical",
    [
        ("1 131", "1131"),
        ("55\u00a0068", "55068"),
        ("1\u202f540,00", "1540"),
        ("01,20", "1.2"),
        ("0003", "3"),
        ("0,0300", "0.03"),
    ],
)
def test_number_presentation_equivalence(source, canonical):
    assert canonical in numeric_values(source)
    assert check(canonical, source_text=source) == []


def test_thousands_are_not_split_or_added_and_decimals_do_not_round():
    assert check("1131 квартира и 55068 м²", source_text="1 131 квартира, 55 068 м²") == []
    assert check("1132 квартиры", source_text="1 131 квартира")
    assert check("131 квартира", source_text="1 131 квартира")
    assert check("4 квартиры", source_text="2 квартиры и 2 квартиры")
    assert check("1,3 процента", source_text="1,29 процента")


@pytest.mark.parametrize(
    "literal,date",
    [
        ("31 марта 2025", "2025-03-31"),
        ("апрелю 2026 года", "2026-04"),
        ("Обновлено 12 ноября 2026", "2026-11-12"),
        ("31.03.2025", "2025-03-31"),
        ("2025-03-31", "2025-03"),
        ("I квартал 2026", "2026-Q1"),
        ("Ключи в дек. 2027", "2027-12"),
        ("28 окт. 2026", "2026-10-28"),
    ],
)
def test_explicit_dates_preserve_precision(literal, date):
    assert check("Срок опубликован", date, source_text=literal) == []


def test_month_quarter_and_unrelated_number_cannot_supply_missing_day():
    assert check("Срок 1 апреля 2026", "2026-04-01", source_text="К апрелю 2026 сдаётся корпус 1")
    assert check("Дата 31 марта 2026", "2026-03-31", source_text="I квартал 2026, дом 31")
    assert check("Срок 2026-03-31", None, source_text="2026-Q1")
    assert check("Срок", "2026-04-31", source_text="апрель 2026")


def test_partial_day_month_requires_explicit_publication_year():
    assert (
        check(
            "Прошёл митинг",
            "2025-03-15",
            source_text="15 марта прошёл митинг",
            published_at="2025-03-31",
        )
        == []
    )
    assert check("Прошёл митинг", "2025-03-15", source_text="15 марта прошёл митинг, корпус 2025")


def test_metadata_does_not_use_url_checked_at_or_random_title_dates():
    assert check("Сообщение", "2025-03-31", source_text="Новость", published_at="2025-03-31") == []
    assert check(
        "Сообщение",
        "2025-03-31",
        source_text="Новость",
        checked_at="2025-03-31",
        url="https://example.org/2025/03/31",
        title="Срок ввода 31 марта 2025",
    )


def test_date_range_is_only_existing_years_in_own_source():
    assert check("Срок 2027–2028 годы", "2027–2028", source_text="Сдача в 2027-2028") == []
    assert check("Зимой 2024–2025", "2024-2025", source_text="Зимой 2024–2025 года") == []
    assert check("Срок 2027–2029", "2027-2029", source_text="Сдача в 2027–2028")
    assert validate_fact_numbers(
        {"text": "55 квартир", "date": None, "source_n": 2}, {"n": 1, "text": "55 квартир"}
    )


def test_word_numerals_are_not_new_quantities():
    assert check("Более двух лет, первая очередь", source_text="Более 2 лет, 1-я очередь") == []
    assert check("8 миллиардов", source_text="Восемь миллиардов") == []


def test_utc_metadata_uses_moscow_day_without_modifying_source():
    source = {"n": 1, "text": "Новость", "published_at": "2026-11-15T22:00:00Z"}
    assert (
        validate_fact_numbers({"text": "Новость", "date": "2026-11-16", "source_n": 1}, source)
        == []
    )
    assert source["published_at"] == "2026-11-15T22:00:00Z"


@pytest.mark.parametrize("suffix", [" года", " год", " г."])
def test_year_suffix_is_formatting_not_a_new_date(suffix):
    assert check("Срок сдачи", "2 квартал 2027" + suffix, source_text="Сдача во II кв. 2027") == []
    assert check("Опубликовано", "23 марта 2026" + suffix, source_text="23 марта 2026") == []
    assert check("Опубликовано", "23 марта 2026" + suffix, source_text="25 марта 2026")
    assert check("Срок сдачи", "2 квартал 2027" + suffix, source_text="2027 год")
    assert check("Срок сдачи", "30 июня 2027" + suffix, source_text="II квартал 2027 года")


def test_suffix_does_not_hide_a_second_date_or_prose():
    assert check("Срок", "2 квартал 2027 года и март 2028", source_text="II кв. 2027 и март 2028")
    assert check("Срок", "2 квартал 2027 года, возможно", source_text="II кв. 2027")


def test_roman_phase_numbers_are_only_explicit_ordinals():
    assert check("Строится 2 очередь", source_text="Строится II очередь") == []
    assert check("Строится 3 очередь", source_text="Строится II очередь")
    assert check("Дом 2", source_text="Класс II, проект ABC")


def test_proven_iso_datetime_is_a_date_without_inventing_time_or_timezone():
    source = {"n": 1, "text": "Сообщение", "published_at": "2026-11-03T06:36:06+00:00"}
    for value in ("2026-11-03T06:36:06Z", "2026-11-03T09:36:06+03:00"):
        assert canonical_fact_date(value, source) == "2026-11-03"
        assert (
            validate_fact_numbers({"text": "Сообщение", "date": value, "source_n": 1}, source) == []
        )
    for value in ("2026-11-03T06:37:06Z", "2026-11-03T06:36:06"):
        assert canonical_fact_date(value, source) == value
        assert validate_fact_numbers({"text": "Сообщение", "date": value, "source_n": 1}, source)
    assert source["published_at"] == "2026-11-03T06:36:06+00:00"


def test_iso_proof_uses_moscow_day_and_does_not_assign_a_zone_to_naive_data():
    stamp = "2026-11-03T22:36:06Z"
    source = {"n": 1, "text": "Сообщение: " + stamp}
    assert canonical_fact_date(stamp, source) == "2026-11-04"
    assert validate_fact_numbers({"text": "Сообщение", "date": stamp, "source_n": 1}, source) == []
    naive = "2026-11-03T22:36:06"
    source = {"n": 1, "text": "Сообщение", "published_at": naive}
    assert canonical_fact_date(naive, source) == "2026-11-03"
    assert canonical_fact_date(stamp, source) == stamp


def test_day_only_or_checked_at_metadata_does_not_prove_a_timestamp():
    stamp = "2026-11-03T06:36:06Z"
    for metadata in ({"published_at": "2026-11-03"}, {"checked_at": stamp}):
        assert check("Сообщение", stamp, source_text="Сообщение", **metadata)
