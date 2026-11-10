from pathlib import Path

import pytest
import yaml


@pytest.fixture
def cfg():
    return yaml.safe_load(Path("config/scoring.yaml").read_text())


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    import socket

    import httpx

    def blocked(*args, **kwargs):
        raise AssertionError("Tests must use an injected network transport")

    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
