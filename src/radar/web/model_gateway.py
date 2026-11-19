"""A private route for the model gateway; all other services keep their own route."""

import os
from urllib.parse import urlsplit

import httpx

PROXY_KEY = "RADAR_OPENROUTER_PROXY_URL"


class ModelCallError(RuntimeError):
    """Safe transport failure, separate from an editorial validation failure."""


class GatewayHTTPError(ValueError):
    """Safe typed HTTP rejection. Upstream bodies/keys never enter the exception."""

    def __init__(self, message, *, status_code, service_host, retry_after=None):
        super().__init__(message)
        self.status_code = status_code
        self.service_host = service_host
        self.retry_after = retry_after
        self.retryable = service_host == "openrouter.ai" and status_code in {429, 502, 503, 504}


def routed_client(cfg=None, **options):
    cfg = os.environ if cfg is None else cfg
    proxy = cfg.get(PROXY_KEY, "").strip()
    mounts = dict(options.pop("mounts", {}) or {})
    if proxy:
        try:
            address = urlsplit(proxy)
            port = address.port
        except ValueError:
            raise ValueError("Некорректный локальный маршрут моделей") from None
        if (
            address.scheme not in {"http", "https"}
            or address.hostname not in {"127.0.0.1", "localhost", "::1"}
            or not port
            or address.username
            or address.password
            or address.path not in {"", "/"}
            or address.query
            or address.fragment
        ):
            raise ValueError("Маршрут моделей должен использовать локальный HTTP-прокси")
        # HTTPS remains end-to-end verified after CONNECT. Never inherit a global proxy.
        mounts["https://openrouter.ai"] = httpx.HTTPTransport(proxy=proxy, verify=True)
    options.setdefault("trust_env", False)
    options.setdefault("verify", True)
    return httpx.Client(mounts=mounts, **options)


def gateway_error(response, service):
    """Classify provider errors without echoing upstream text or credentials."""
    if (
        response.status_code == 429
        and response.request.url.host == "searchapi.api.cloud.yandex.net"
    ):
        return (
            f"{service}: поиск временно ограничил частоту запросов (429). "
            "Повторите поиск позже. Сохранённые источники не потеряны."
        )
    if response.request.url.host != "openrouter.ai":
        return None
    if response.status_code == 429:
        return (
            f"{service}: сервис моделей временно ограничил частоту запросов (429). "
            "Сохранённые материалы не потеряны; повтор возможен после ожидания."
        )
    if response.status_code in {502, 503, 504}:
        return (
            f"{service}: сервис моделей временно недоступен ({response.status_code}). "
            "Сохранённые материалы не потеряны."
        )
    if response.status_code == 400:
        return (
            f"{service}: сервис моделей не принял формат или настройки запроса (400). "
            "Требуется скорректировать запрос к модели."
        )
    if response.status_code == 401:
        return (
            f"{service}: сервис моделей не принял ключ доступа (401). "
            "Проверьте ключ в настройках подключения."
        )
    if response.status_code == 402:
        return (
            f"{service}: недостаточно средств для запроса к модели (402). "
            "Проверьте баланс сервиса моделей."
        )
    if response.status_code != 403:
        return None
    try:
        value = response.json().get("error", {})
        message = str(value.get("message", "") if isinstance(value, dict) else value).lower()
    except (ValueError, TypeError, AttributeError):
        message = ""
    if "security policy" in message or "access denied" in message:
        return (
            f"{service}: сервис моделей отклонил подключение сервера (403). "
            "Нужно восстановить сетевой доступ к моделям. Сохранённые отчёты не потеряны."
        )
    return (
        f"{service}: сервис моделей отклонил запрос (403). "
        "Проверьте доступ к выбранной модели и сетевой маршрут сервера."
    )
