import pytest

from radar.web import telegram


def test_existing_reader_keeps_direct_route_when_proxy_is_not_configured(monkeypatch):
    captured = {}
    monkeypatch.setattr(telegram, "TelegramClient", lambda *args, **kw: captured.update(kw))
    monkeypatch.setattr(telegram, "StringSession", lambda value: object())
    telegram.factory({"TG_API_ID": "123", "TG_API_HASH": "test"}, "test")
    assert captured["proxy"] is None
    assert captured["receive_updates"] is False
    assert captured["request_retries"] == 0


def test_reader_uses_only_its_explicit_proxy_setting(monkeypatch):
    captured = {}
    monkeypatch.setattr(telegram, "TelegramClient", lambda *args, **kw: captured.update(kw))
    monkeypatch.setattr(telegram, "StringSession", lambda value: object())
    telegram.factory(
        {
            "TG_API_ID": "123",
            "TG_API_HASH": "test",
            "RADAR_TELEGRAM_PROXY_URL": "http://127.0.0.1:10890",
            "HTTP_PROXY": "http://unrelated.invalid:8080",
        },
        "test",
    )
    assert captured["proxy"] == {
        "proxy_type": "http", "addr": "127.0.0.1", "port": 10890,
        "rdns": True, "username": None, "password": None,
    }


@pytest.mark.parametrize("value", [
    "https://127.0.0.1:10890", "http://127.0.0.1", "http://127.0.0.1:bad",
    "socks5://127.0.0.1:10891/path", "http://127.0.0.1:10890?key=secret",
])
def test_invalid_proxy_configuration_has_no_secret_in_error(value):
    with pytest.raises(ValueError, match="Некорректная настройка маршрута Telegram") as error:
        telegram.proxy_options(value)
    assert "secret" not in str(error.value)
