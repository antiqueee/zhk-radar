from concurrent.futures import ThreadPoolExecutor
from shutil import copytree
from threading import Event

from radar.web import workflow
from radar.web.store import Store


def test_distinct_cache_keys_can_complete_during_a_slow_paid_request(tmp_path, monkeypatch):
    copytree("config", tmp_path / "config")
    store = Store(tmp_path)
    started, finish = Event(), Event()
    charges = []
    monkeypatch.setattr(workflow, "reserve_call", lambda _store, run, _service: charges.append(run))

    def slow_call():
        started.set()
        assert finish.wait(5)
        return {"source": "first"}

    def duplicate_call():
        raise AssertionError("same cache key started a second paid request")

    with ThreadPoolExecutor(max_workers=3) as threads:
        first = threads.submit(workflow.cached, store, ["slow"], slow_call, "first", "model")
        try:
            assert started.wait(5)
            duplicate = threads.submit(
                workflow.cached, store, ["slow"], duplicate_call, "duplicate", "model"
            )
            second = threads.submit(
                workflow.cached, store, ["quick"], lambda: {"source": "second"}, "second", "model"
            )
            assert second.result(timeout=5) == ({"source": "second"}, False)
        finally:
            finish.set()

        assert first.result(timeout=5) == ({"source": "first"}, False)
        assert duplicate.result(timeout=5) == ({"source": "first"}, True)

    assert charges == ["first", "second"]
    workflow.discard_cached(store, ["slow"], {"source": "older"})
    assert workflow.cached(store, ["slow"], duplicate_call, "duplicate", "model")[1] is True
    workflow.discard_cached(store, ["slow"], {"source": "first"})
    assert workflow.cached(store, ["slow"], lambda: "updated", "retry", "model") == (
        "updated", False,
    )
