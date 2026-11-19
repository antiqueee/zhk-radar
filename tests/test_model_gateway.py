import httpx
import pytest

from radar.web.discovery import call_json
from radar.web.model_gateway import PROXY_KEY, GatewayHTTPError, gateway_error, routed_client


def test_private_route_only_carries_model_gateway_https(monkeypatch):
    proxied, direct, tls = [], [], []

    def private_transport(**options):
        tls.append(options["verify"])
        assert options["proxy"] == "http://127.0.0.1:10890"
        return httpx.MockTransport(
            lambda request: proxied.append(str(request.url)) or httpx.Response(200)
        )

    monkeypatch.setenv("HTTPS_PROXY", "http://unrelated-proxy.invalid:9999")
    monkeypatch.setattr("radar.web.model_gateway.httpx.HTTPTransport", private_transport)
    transport = httpx.MockTransport(
        lambda request: direct.append(str(request.url)) or httpx.Response(200)
    )
    with routed_client({PROXY_KEY: "http://127.0.0.1:10890"}, transport=transport) as client:
        for url in (
            "https://openrouter.ai/api/v1/models",
            "https://searchapi.api.cloud.yandex.net/v2/web/search",
            "https://openrouter.ai.evil.invalid/",
            "https://sub.openrouter.ai/",
            "http://openrouter.ai/",
        ):
            client.get(url)
    assert proxied == ["https://openrouter.ai/api/v1/models"]
    assert len(direct) == 4
    assert tls == [True]  # The CONNECT tunnel never disables upstream TLS validation.


@pytest.mark.parametrize(
    "proxy",
    ["http://public.example:10890", "socks5://127.0.0.1:10890", "http://secret@127.0.0.1:10890"],
)
def test_private_route_rejects_unintended_destinations_without_echoing_credentials(proxy):
    with pytest.raises(ValueError) as error:
        routed_client({PROXY_KEY: proxy})
    assert proxy not in str(error.value)
    assert "secret" not in str(error.value)


def test_security_policy_error_is_not_misreported_as_a_bad_key_or_balance():
    response = {
        "error": {"message": "Access denied by security policy. leaked-secret", "code": 403}
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(403, json=response))
    with httpx.Client(transport=transport) as client, pytest.raises(ValueError) as error:
        call_json(client, "https://openrouter.ai/api/v1/chat/completions", {}, {}, "Отчёт: facts")
    assert "подключение сервера" in str(error.value)
    assert "баланс" not in str(error.value)
    assert "Проверьте ключ" not in str(error.value)
    assert "leaked-secret" not in str(error.value)


@pytest.mark.parametrize(
    "status,expected,excluded",
    [
        (400, "формат или настройки запроса", ["ключ", "баланс"]),
        (401, "ключ доступа", ["баланс", "формат"]),
        (402, "недостаточно средств", ["ключ", "формат"]),
    ],
)
def test_openrouter_failure_messages_distinguish_format_credentials_and_balance(
    status, expected, excluded
):
    body = {"error": {"message": "leaked-secret", "metadata": {"raw": "private-details"}}}
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json=body))
    with httpx.Client(transport=transport) as client, pytest.raises(ValueError) as error:
        call_json(client, "https://openrouter.ai/api/v1/chat/completions", {}, {}, "Отчёт")
    message = str(error.value)
    assert expected in message and str(status) in message
    assert all(value not in message for value in [*excluded, "leaked-secret", "private-details"])


@pytest.mark.parametrize("status", [400, 401, 402, 403])
def test_openrouter_error_classification_does_not_apply_to_other_services(status):
    response = httpx.Response(
        status,
        request=httpx.Request("POST", "https://searchapi.api.cloud.yandex.net/v2/web/search"),
    )
    assert gateway_error(response, "Поиск") is None


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_explicit_temporary_provider_rejection_preserves_safe_retry_metadata(status):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            status,
            headers={"Retry-After": "5"},
            json={"error": {"message": "leaked-secret", "metadata": {"raw": "private-details"}}},
        )
    )
    with httpx.Client(transport=transport) as client, pytest.raises(GatewayHTTPError) as error:
        call_json(client, "https://openrouter.ai/api/v1/chat/completions", {}, {}, "Отчёт")
    assert error.value.status_code == status
    assert error.value.retry_after == "5" and error.value.retryable
    assert "leaked-secret" not in str(error.value) and "баланс" not in str(error.value)
