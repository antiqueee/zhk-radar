"""No network or actual sleeping: retry only unambiguous transient HTTP errors."""

from datetime import UTC, datetime

import pytest

from radar.reports.model_retry import retry_model_call


class HTTPFailure(ValueError):
    def __init__(self, status, after=None, retryable=None):
        super().__init__("Transport failure")
        self.status_code, self.retry_after, self.retryable = status, after, retryable


def operation(values):
    remaining, calls = iter(values), []

    def run():
        calls.append(True)
        value = next(remaining)
        if isinstance(value, Exception):
            raise value
        return value

    return run, calls


def test_success_is_returned_without_wait_or_callback():
    run, calls = operation([{"ok": True}])
    waits, updates = [], []
    assert retry_model_call(
        run, lambda **row: updates.append(row), sleep=waits.append
    ) == {"ok": True}
    assert len(calls) == 1 and waits == updates == []


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_explicit_transient_http_retries_then_succeeds(status):
    run, calls = operation([HTTPFailure(status), {"ok": True}])
    waits, updates = [], []
    assert retry_model_call(
        run, lambda **row: updates.append(row), sleep=waits.append
    ) == {"ok": True}
    assert len(calls) == 2 and waits == [2.0]
    assert updates == [{"retry_number": 1, "delay": 2.0, "status_code": status}]


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 408, 409, 500])
def test_auth_balance_policy_schema_and_other_http_failures_are_never_retried(status):
    error = HTTPFailure(status, retryable=True)
    run, calls = operation([error])
    waits = []
    with pytest.raises(HTTPFailure) as caught:
        retry_model_call(run, sleep=waits.append)
    assert caught.value is error and len(calls) == 1 and waits == []


@pytest.mark.parametrize("error", [
    ValueError("HTTP 429; retry this error"),
    TimeoutError("Read timed out; server might already have generated the answer"),
    HTTPFailure("429"),
    HTTPFailure(429, retryable=False),
])
def test_untyped_ambiguous_or_explicitly_disabled_failure_is_not_retried(error):
    run, calls = operation([error])
    waits = []
    with pytest.raises(type(error)) as caught:
        retry_model_call(run, sleep=waits.append)
    assert caught.value is error and len(calls) == 1 and waits == []


def test_two_retries_three_requests_is_the_absolute_limit_and_last_error_is_preserved():
    failures = [HTTPFailure(429), HTTPFailure(503), HTTPFailure(504)]
    run, calls = operation(failures)
    waits, updates = [], []
    with pytest.raises(HTTPFailure) as caught:
        retry_model_call(run, lambda **row: updates.append(row), sleep=waits.append)
    assert caught.value is failures[-1] and len(calls) == 3
    assert waits == [2.0, 4.0] and [row["retry_number"] for row in updates] == [1, 2]


def test_retry_after_is_a_minimum_and_not_ignored():
    run, calls = operation([HTTPFailure(429, "7"), HTTPFailure(503, 8.5), "done"])
    waits = []
    assert retry_model_call(run, sleep=waits.append) == "done"
    assert len(calls) == 3 and waits == [7.0, 8.5]


def test_http_date_retry_after_uses_injected_clock():
    current = datetime(2026, 11, 17, 8, 0, 0, tzinfo=UTC)
    run, _ = operation([HTTPFailure(429, "Tue, 17 Nov 2026 08:00:12 GMT"), "done"])
    waits = []
    assert retry_model_call(run, sleep=waits.append, now=lambda: current) == "done"
    assert waits == [12.0]


@pytest.mark.parametrize("value", [None, "", "not a date", "-1", float("inf"), float("nan")])
def test_bad_retry_after_uses_bounded_backoff(value):
    run, _ = operation([HTTPFailure(429, value), "done"])
    waits = []
    assert retry_model_call(run, sleep=waits.append) == "done"
    assert waits == [2.0]


def test_retry_after_above_single_wait_cap_is_not_followed_by_an_early_retry():
    error = HTTPFailure(429, "31")
    run, calls = operation([error])
    waits = []
    with pytest.raises(HTTPFailure) as caught:
        retry_model_call(run, sleep=waits.append)
    assert caught.value is error and len(calls) == 1 and waits == []


def test_total_wait_cap_never_shrinks_retry_after_into_an_early_request():
    errors = [HTTPFailure(429, 30), HTTPFailure(429, 20)]
    run, calls = operation(errors)
    waits = []
    with pytest.raises(HTTPFailure) as caught:
        retry_model_call(run, sleep=waits.append)
    assert caught.value is errors[1] and len(calls) == 2 and waits == [30.0]
    assert sum(waits) <= 45


def test_full_45_second_total_budget_and_30_second_individual_cap_can_be_used():
    run, calls = operation([HTTPFailure(429, 30), HTTPFailure(503, 15), "done"])
    waits = []
    assert retry_model_call(run, sleep=waits.append) == "done"
    assert waits == [30.0, 15.0] and sum(waits) == 45 and len(calls) == 3


def test_retry_limit_can_be_lowered_and_absolute_wait_ceilings_cannot_be_exceeded():
    run, calls = operation([HTTPFailure(429), HTTPFailure(429)])
    waits = []
    with pytest.raises(HTTPFailure):
        retry_model_call(run, max_retries=1, sleep=waits.append)
    assert len(calls) == 2 and waits == [2.0]
    for kwargs in [{"max_retries": 3}, {"delay_cap": 61}, {"total_wait_cap": 91}]:
        with pytest.raises(ValueError):
            retry_model_call(lambda: "must not run", **kwargs)


def test_explicit_background_override_respects_60_second_retry_after_and_total_90():
    run, calls = operation([HTTPFailure(429, 60), HTTPFailure(503, 30), "done"])
    waits = []
    assert retry_model_call(
        run, sleep=waits.append, delay_cap=60, total_wait_cap=90
    ) == "done"
    assert waits == [60.0, 30.0] and sum(waits) == 90 and len(calls) == 3


def test_background_override_does_not_retry_early_past_remaining_90_second_budget():
    errors = [HTTPFailure(429, 60), HTTPFailure(429, 31)]
    run, calls = operation(errors)
    waits = []
    with pytest.raises(HTTPFailure) as caught:
        retry_model_call(run, sleep=waits.append, delay_cap=60, total_wait_cap=90)
    assert caught.value is errors[-1] and waits == [60.0] and len(calls) == 2


def test_60_second_header_is_not_enabled_without_explicit_override():
    error = HTTPFailure(429, 60)
    run, calls = operation([error])
    waits = []
    with pytest.raises(HTTPFailure) as caught:
        retry_model_call(run, sleep=waits.append)
    assert caught.value is error and waits == [] and len(calls) == 1


def test_schema_or_author_failure_after_successful_http_is_outside_retry_boundary():
    run, calls = operation([{"malformed_author_data": True}])
    waits = []
    # Transport returns once. Schema validation belongs to the caller after this return.
    value = retry_model_call(run, sleep=waits.append)
    assert value == {"malformed_author_data": True} and len(calls) == 1 and waits == []
