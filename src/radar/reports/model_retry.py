"""Bounded retries for explicitly typed, transient model HTTP failures only.

This adapter surrounds a single transport operation. Parsed/model-authored data,
schema/critic failures, authentication/balance/policy failures and ambiguous
timeouts are outside its retry boundary. There is no dependency on a model SDK.
"""

import math
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
MAX_RETRIES = 2
MAX_DELAY = 30.0
MAX_TOTAL_WAIT = 45.0
ABSOLUTE_MAX_DELAY = 60.0
ABSOLUTE_MAX_TOTAL_WAIT = 90.0


def _now():
    return datetime.now(UTC)


def retry_status(error):
    """Do not infer transport status from an error message or retryable=True alone."""
    status = getattr(error, "status_code", None)
    if type(status) is not int or status not in RETRYABLE_STATUSES:
        return None
    if getattr(error, "retryable", None) is False:
        return None
    return status


def _seconds(value, now):
    if value is None or isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        if not isinstance(value, str):
            return None
        try:
            moment = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        current = now()
        if isinstance(current, (int, float)):
            current = datetime.fromtimestamp(current, UTC)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        return max(0.0, (moment - current).total_seconds())
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def retry_model_call(
    operation,
    on_wait=None,
    *,
    sleep=time.sleep,
    now=_now,
    max_retries=MAX_RETRIES,
    default_delay=2.0,
    delay_cap=MAX_DELAY,
    total_wait_cap=MAX_TOTAL_WAIT,
):
    """Return operation(), or propagate its unchanged final exception.

    Typed exceptions carry integer status_code, optional retry_after (seconds or
    HTTP-date), and optional retryable=False. on_wait is called with keyword
    retry_number (1/2), delay and status_code before an actual scheduled wait.

    Default waits are bounded by 30 seconds each and 45 seconds in total. A
    background caller may explicitly raise these caps to at most 60/90 seconds.
    At most two retries/three requests are allowed with either configuration.
    Retry-After is a minimum: when that minimum cannot fit either configured cap,
    do not retry too early. Unknown/invalid Retry-After uses exponential fallback.
    """
    if type(max_retries) is not int or not 0 <= max_retries <= MAX_RETRIES:
        raise ValueError("Допустимо не более двух повторов модельного HTTP-запроса")
    for name, value, upper in (
        ("delay_cap", delay_cap, ABSOLUTE_MAX_DELAY),
        ("total_wait_cap", total_wait_cap, ABSOLUTE_MAX_TOTAL_WAIT),
    ):
        if isinstance(value, bool) or not math.isfinite(value) or not 0 <= value <= upper:
            raise ValueError("Недопустимый предел ожидания: " + name)
    if isinstance(default_delay, bool) or not math.isfinite(default_delay) or default_delay < 0:
        raise ValueError("Недопустимое базовое ожидание")
    retries, waited = 0, 0.0
    while True:
        try:
            return operation()
        except Exception as error:
            status = retry_status(error)
            if status is None or retries >= max_retries:
                raise
            after = _seconds(getattr(error, "retry_after", None), now)
            delay = min(default_delay * (2 ** retries), delay_cap)
            if after is not None:
                delay = max(delay, after)
            if delay > delay_cap or delay > total_wait_cap - waited:
                raise
            retries += 1
            if on_wait is not None:
                on_wait(retry_number=retries, delay=delay, status_code=status)
            sleep(delay)
            waited += delay
