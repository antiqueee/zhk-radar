import shutil

import pytest

from radar.reports import pipeline
from radar.web import benchmark
from radar.web.configuration import write_secrets
from radar.web.discovery import Findings
from radar.web.model_gateway import PROXY_KEY
from radar.web.store import Store


class FakeClient:
    def __init__(self):
        self.closed = False

    def __bool__(self):
        return False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self):
        self.closed = True

    def post(self, *_args, **_kwargs):
        pytest.fail("Route/ownership tests must not call the network")


@pytest.fixture
def store(tmp_path, monkeypatch):
    shutil.copytree("config", tmp_path / "config")
    write_secrets(
        tmp_path,
        {
            "OPENROUTER_API_KEY": "fixture",
            "YANDEX_SEARCH_API_KEY": "fixture",
            "YANDEX_FOLDER_ID": "fixture",
            "PERPLEXITY_API_KEY": "fixture",
            PROXY_KEY: "http://127.0.0.1:10890",
        },
    )
    monkeypatch.setattr(benchmark, "reserve_call", lambda *_args: None)
    return Store(tmp_path)


def test_benchmark_routes_only_model_host_without_replacing_adapters(store, monkeypatch):
    client, factories, searches, extractions = FakeClient(), [], [], []

    def factory(cfg, **options):
        factories.append((cfg[PROXY_KEY], options["timeout"]))
        return client

    def search(actual, cfg, provider, query):
        assert actual is client
        searches.append(provider)
        return [{"url": "https://example.org/source", "title": "ЖК", "text": "Источник"}]

    def extract(actual, cfg, model, documents):
        assert actual is client and cfg[PROXY_KEY] == "http://127.0.0.1:10890"
        extractions.append(model["model"])
        return Findings(objects=[])

    monkeypatch.setattr(benchmark, "routed_client", factory)
    monkeypatch.setattr(
        benchmark, "model_config", lambda _root: {"benchmark_a": {"model": "test/a"}}
    )
    runner = benchmark.BenchmarkRunner(store, search=search, extractor=extract)
    # Execute synchronously; execute() only updates a submitted job's row at the end.
    runner.execute(
        "route-test", benchmark.BenchmarkRequest(queries=["ЖК"], model_roles=["benchmark_a"])
    )
    assert searches == ["yandex", "perplexity"] and extractions == ["test/a"]
    assert client.closed and len(factories) == 1
    assert factories[0][0] == "http://127.0.0.1:10890"
    assert factories[0][1].connect == 15 and factories[0][1].read == 90


@pytest.mark.parametrize("injected", [False, True])
def test_legacy_report_factory_receives_config_and_preserves_injection(
    store, monkeypatch, injected
):
    class ReachedRunner(RuntimeError):
        pass

    client, factories = FakeClient(), []

    def factory(cfg, **options):
        assert not injected, "Explicit client must not be recreated"
        factories.append((cfg[PROXY_KEY], options["timeout"]))
        return client

    def runner(actual_store, run_id, actual_client, invoke):
        assert actual_store is store and actual_client is client
        raise ReachedRunner

    monkeypatch.setattr(pipeline, "routed_client", factory)
    monkeypatch.setattr(pipeline, "Runner", runner)
    monkeypatch.setattr(pipeline, "payload_for", lambda *_args: {"dataset": "web", "name": "ЖК"})
    monkeypatch.setattr(
        pipeline, "select_sources", lambda _payload: [{"url": "https://example.org"}]
    )
    monkeypatch.setattr(pipeline, "services", lambda _root: ({}, {}))
    monkeypatch.setattr(
        pipeline,
        "model_config",
        lambda _root: {"author": {"model": "test/a"}, "review": {"model": "test/b"}},
    )
    with pytest.raises(ReachedRunner):
        pipeline.generate(store, "demo-1", "route-test", client=client if injected else None)
    assert client.closed is not injected
    assert factories == ([] if injected else [("http://127.0.0.1:10890", 180)])
