import base64
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from radar.web import discovery

CFG = {"YANDEX_SEARCH_API_KEY": "test", "YANDEX_FOLDER_ID": "test-folder"}


class FakeClock:
    def __init__(self):
        self.value = 100.0
        self.sleeps = []

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


def test_workers_share_one_paced_queue_with_fake_time_and_no_extra_api_requests(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(
        discovery, "YANDEX_PACER", discovery.YandexPacer(clock=clock, sleep=clock.sleep)
    )
    starts, queries = [], []
    xml = base64.b64encode(b"<yandexsearch><response><results/></response></yandexsearch>").decode()

    def handler(request):
        starts.append(clock())
        queries.append(json.loads(request.content)["query"]["queryText"])
        return httpx.Response(200, json={"rawData": xml})

    barrier = threading.Barrier(2)

    def worker(label):
        barrier.wait()
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            for index in range(3):
                assert discovery.yandex(client, CFG, f"{label}-{index}", "month") == []

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(worker, ["first", "second"]))
    assert len(starts) == 6 and len(set(queries)) == 6
    assert all(later - earlier >= 1 for earlier, later in zip(starts, starts[1:], strict=False))
    assert clock.sleeps == [1.0] * 5


def test_error_still_spends_one_start_slot_and_429_never_retries_or_blames_credentials(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(
        discovery, "YANDEX_PACER", discovery.YandexPacer(clock=clock, sleep=clock.sleep)
    )
    starts = []

    def handler(request):
        starts.append(clock())
        return httpx.Response(429, json={"error": "secret account-details"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        for _ in range(2):
            with pytest.raises(ValueError, match="временно ограничил частоту") as error:
                discovery.yandex(client, CFG, "query", "month")
            assert all(
                word not in str(error.value)
                for word in ("ключ", "баланс", "secret", "account-details")
            )
    assert starts == [100.0, 101.0]


def test_early_wakeup_rechecks_monotonic_clock():
    clock = FakeClock()
    early = True

    def sleep(seconds):
        nonlocal early
        clock.value += seconds / 2 if early else seconds
        early = False

    pacer = discovery.YandexPacer(clock=clock, sleep=sleep)
    with pacer.slot():
        first = clock()
    with pacer.slot():
        assert clock() - first >= 1.0


def test_slow_request_does_not_add_unnecessary_delay_and_failure_releases_lock():
    clock = FakeClock()
    pacer = discovery.YandexPacer(clock=clock, sleep=clock.sleep)
    with pytest.raises(RuntimeError), pacer.slot():
        clock.value += 2.0
        raise RuntimeError("transport failure")
    with pacer.slot():
        assert clock.value == 102.0
    assert clock.sleeps == []
