from datetime import date

import pytest

from radar.pipeline.evidence import dedup_key, validate_quote


@pytest.mark.parametrize("quote", ["", " ", "срок сорван", "Перенос срока"])
def test_rejects_unsupported_quote(quote):
    with pytest.raises(ValueError):
        validate_quote("Срок передачи перенесён на 31 декабря.", quote)


def test_quote_and_week_dedup():
    validate_quote("Срок передачи перенесён на 31 декабря.", "на 31 декабря")
    assert dedup_key("shift", "object", "1", date(2026, 11, 9)) == dedup_key(
        "shift", "object", "1", date(2026, 11, 15)
    )
    assert dedup_key("shift", "object", "1", date(2026, 11, 9)) != dedup_key(
        "shift", "object", "2", date(2026, 11, 9)
    )
    assert dedup_key("shift", "object", "1", date(2026, 11, 9)) != dedup_key(
        "shift", "complex", "1", date(2026, 11, 9)
    )
    assert dedup_key("shift", "object", "1", date(2026, 11, 9)) != dedup_key(
        "shift", "object", "1", date(2026, 11, 16)
    )
