"""Read-only input adapters for Brand Analytics message data."""

import csv
import io
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from dateutil.parser import isoparse, parse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    field_validator,
    model_validator,
)

_MAX_PAGE_BYTES = 8 * 1024 * 1024
_MAX_EXPORT_BYTES = 32 * 1024 * 1024
_MAX_EXPORT_ROWS = 50_000
_OFFICIAL_DOMAINS = ("brandanalytics.ru", "br-analytics.ru")
_MISSING = object()
_CALENDAR_DAY = re.compile(r"^(?:\d{4}-\d{2}-\d{2}|\d{1,2}[./]\d{1,2}[./]\d{4})(?:[T ]|$)")
_MESSAGE_PATHS = ("messages", "items", "data.messages", "data.items", "data", "result.messages")
_CURSOR_PATHS = ("next_cursor", "nextCursor", "pagination.next_cursor", "data.next_cursor")
_MORE_PATHS = ("has_more", "hasMore", "pagination.hasMore", "pagination.has_more", "data.has_more")
_SNIPPET_FIELDS = ("snippet", "Сниппет", "Сниппет сообщения")
_ALIASES = {
    "provider_id": (
        "provider_id", "id", "message_id", "messageId", "ID сообщения", "Hash сообщения",
        "Хэш сообщения",
    ),
    "url": (
        "original_url", "url", "link", "Ссылка на сообщение", "Ссылка на публикацию", "Ссылка",
        "URL сообщения",
    ),
    "text": ("full_text", "text", "message", "Полный текст", "Текст сообщения", "Текст"),
    "title": ("title", "Заголовок"),
    "published_at": (
        "published_at", "date", "created_at", "Дата и время публикации", "Дата публикации", "Дата",
    ),
    "platform": ("platform", "Площадка", "Социальная сеть", "Источник"),
    "region": ("region", "Город", "Регион"),
    "contour": ("contour",),
    "source_kind": ("source_kind",),
    "author": ("author.name", "author", "Автор"),
    "truncated": ("truncated", "is_truncated", "is_snippet", "Сокращённый текст"),
}


class TransportError(ValueError):
    """A safe operator-facing error without upstream bodies or settings values."""

    def __init__(self, message, *, status_code=None, retry_after=None, retryable=False):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
        self.retryable = retryable


class RequestFields(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    topic_id: str | None = "topic_id"
    since: str = "since"
    until: str | None = None
    cursor: str | None = "cursor"
    limit: str | None = "limit"

    @field_validator("*")
    @classmethod
    def valid_parameter(cls, value):
        if value is not None and not re.fullmatch(r"[A-Za-z0-9_.\[\]-]{1,64}", value):
            raise ValueError("Неверное имя параметра API Brand Analytics")
        return value


class FeedSettings(BaseModel):
    """Map the documented account endpoint to the service's message input."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    endpoint: str = Field(max_length=512)
    token: SecretStr = Field(repr=False)
    topic_ids: tuple[str, ...] = Field(min_length=1, max_length=2)
    method: str = "GET"
    token_header: str = "Authorization"
    token_scheme: str = "Bearer"
    request_fields: RequestFields = Field(default_factory=RequestFields)
    static_params: dict[str, JsonValue] = Field(default_factory=dict)
    messages_path: str | None = None
    next_cursor_path: str | None = None
    field_map: dict[str, str] = Field(default_factory=dict)
    date_format: str = "iso"
    source_timezone: str = "Europe/Moscow"
    page_size: int = Field(default=100, ge=1, le=500)

    @field_validator("endpoint")
    @classmethod
    def official_endpoint(cls, value):
        try:
            url = urlsplit(value)
            host = (url.hostname or "").casefold()
            official = any(host == domain or host.endswith("." + domain)
                           for domain in _OFFICIAL_DOMAINS)
            valid = (
                url.scheme == "https" and official and url.port in {None, 443}
                and not (url.username or url.password or url.fragment or url.query)
                and url.path not in {"", "/"}
                and not re.search(r"[\x00-\x20\x7f\\{}]", value.replace("{topic_id}", ""))
                and ("{topic_id}" not in value or "{topic_id}" in url.path)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Укажите HTTPS-метод официального API Brand Analytics")
        return value

    @field_validator("token")
    @classmethod
    def valid_token(cls, value):
        if not re.fullmatch(r"[!-~]{1,4096}", value.get_secret_value()):
            raise ValueError("Некорректный ключ Brand Analytics")
        return value

    @field_validator("method")
    @classmethod
    def read_method(cls, value):
        method = value.upper()
        if method not in {"GET", "POST"}:
            raise ValueError("Для выгрузки разрешены методы GET и POST")
        return method

    @field_validator("token_header")
    @classmethod
    def auth_header(cls, value):
        if value.casefold() != "authorization" and not re.fullmatch(
            r"(?:X-[A-Za-z0-9-]{1,56}|API-Key)", value, re.I
        ):
            raise ValueError("Некорректный заголовок авторизации Brand Analytics")
        return value

    @field_validator("token_scheme")
    @classmethod
    def auth_scheme(cls, value):
        if value and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", value):
            raise ValueError("Некорректная схема авторизации Brand Analytics")
        return value

    @field_validator("date_format")
    @classmethod
    def time_format(cls, value):
        if value not in {"iso", "unix", "unix_ms"}:
            raise ValueError("Формат даты Brand Analytics должен быть iso, unix или unix_ms")
        return value

    @field_validator("source_timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("Неизвестный часовой пояс выгрузки Brand Analytics") from None
        return value

    @field_validator("field_map")
    @classmethod
    def valid_mapping(cls, value):
        if value.keys() - _ALIASES.keys() or any(not item.strip() for item in value.values()):
            raise ValueError("Неизвестные или пустые поля сопоставления Brand Analytics")
        return value

    @model_validator(mode="after")
    def consistent_request(self):
        if len(set(self.topic_ids)) != len(self.topic_ids) or any(
            not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", topic) for topic in self.topic_ids
        ):
            raise ValueError("Укажите один или два разных идентификатора темы Brand Analytics")
        if len(self.topic_ids) == 2 and not (
            self.request_fields.topic_id or "{topic_id}" in self.endpoint
        ):
            raise ValueError("Для двух тем нужно передавать идентификатор темы")
        reserved = [value for value in self.request_fields.model_dump().values() if value]
        if len(set(reserved)) != len(reserved) or set(reserved) & self.static_params.keys():
            raise ValueError("Параметры запроса Brand Analytics не должны дублироваться")
        if self.method == "GET" and any(
            isinstance(value, (list, dict)) for value in self.static_params.values()
        ):
            raise ValueError("Статические параметры GET должны быть одиночными значениями")
        return self

    @classmethod
    def from_mapping(cls, settings):
        prefix = "BRAND_ANALYTICS_"
        values = {
            field: settings[prefix + field.upper()]
            for field in cls.model_fields
            if settings.get(prefix + field.upper()) is not None
        }
        values["topic_ids"] = tuple(
            topic.strip() for topic in values.get("topic_ids", "").split(",") if topic.strip()
        )
        for name in ("request_fields", "static_params", "field_map"):
            if name in values:
                try:
                    values[name] = json.loads(values[name] or "{}")
                except (ValueError, TypeError):
                    raise TransportError("Некорректный JSON в настройках Brand Analytics") from None
        for name in ("messages_path", "next_cursor_path"):
            if name in values:
                values[name] = values[name].strip() or None
        return cls.model_validate(values)


@dataclass(frozen=True)
class MessagePage:
    messages: list[dict]
    next_cursor: str | None
    raw_count: int


def _value(data, path):
    if isinstance(data, dict) and path in data:
        return data[path]
    for part in path.split("."):
        if not isinstance(data, dict) or part not in data:
            return _MISSING
        data = data[part]
    return data


def _first_value(data, paths):
    for path in paths:
        candidate = _value(data, path)
        if candidate is not _MISSING:
            return candidate
    return _MISSING


def _date(value, timezone):
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            divisor = 1000 if value > 100_000_000_000 else 1
            return datetime.fromtimestamp(value / divisor, tz=UTC).isoformat()
        if isinstance(value, str) and value.strip():
            value = value.strip()
            if re.fullmatch(r"\d{10}|\d{13}", value):
                return _date(int(value), timezone)
            if not _CALENDAR_DAY.match(value):
                return None
            try:
                parsed = isoparse(value)
            except ValueError:
                parsed = parse(value, dayfirst=value[4:5] != "-")
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone)
            return parsed.astimezone(UTC).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        pass
    return None


def _normalize_rows(rows, field_map, timezone):
    messages = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row = {**row, **{str(key).strip().casefold(): value for key, value in row.items()}}
        normalized = {}
        for key, aliases in _ALIASES.items():
            column = (field_map[key],) if key in field_map else aliases
            value = _first_value(row, (*column, *(name.strip().casefold() for name in column)))
            if value is _MISSING or value is None:
                continue
            if key == "published_at":
                normalized[key] = _date(value, timezone)
            elif key == "truncated":
                normalized[key] = value is True or str(value).strip().casefold() in {
                    "1", "true", "yes", "да",
                }
            elif isinstance(value, (str, int)) and not isinstance(value, bool):
                normalized[key] = str(value).strip()
        if not normalized.get("text") and "text" not in field_map:
            snippet = _first_value(row, (key.casefold() for key in _SNIPPET_FIELDS))
            if isinstance(snippet, str) and snippet.strip():
                normalized["text"] = snippet.strip()
                normalized["truncated"] = True
        if normalized.get("truncated"):
            normalized["source_kind"] = "snippet"
        if normalized.get("url") and normalized.get("text"):
            messages.append(normalized)
    if rows and not messages:
        raise TransportError("В выгрузке Brand Analytics нет исходных ссылок и текстов")
    return messages


def _message_rows(payload, path=None):
    if path:
        rows = _value(payload, path)
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = _first_value(payload, _MESSAGE_PATHS)
    if not isinstance(rows, list):
        raise TransportError("Ответ Brand Analytics не содержит настроенного списка сообщений")
    return rows


def _json(data):
    try:
        return json.loads(data)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise TransportError("Некорректный ответ JSON от Brand Analytics") from None


def _next_cursor(payload, settings, row_count):
    paths = (settings.next_cursor_path,) if settings.next_cursor_path else _CURSOR_PATHS
    value = _first_value(payload, paths)
    has_more = _first_value(payload, _MORE_PATHS)
    if value is _MISSING and (has_more is True or (
        has_more is not False and settings.request_fields.limit and row_count >= settings.page_size
    )):
        raise TransportError("Ответ Brand Analytics не подтвердил последнюю страницу выгрузки")
    if value is _MISSING or value is None or value == "":
        if has_more is True:
            raise TransportError("Brand Analytics сообщил о следующей странице без курсора")
        return None
    if not settings.request_fields.cursor or isinstance(value, bool) or not (
        isinstance(value, (str, int)) and len(str(value)) <= 4096
    ):
        raise TransportError("Не удалось разобрать курсор страницы Brand Analytics")
    return str(value)


def _time_parameter(value, date_format):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TransportError("Время запроса Brand Analytics должно включать часовой пояс")
    value = value.astimezone(UTC)
    if date_format == "iso":
        return value.isoformat()
    return int(value.timestamp() * (1000 if date_format == "unix_ms" else 1))


def _retry_after(value):
    if not value:
        return None
    if value.isdigit() and len(value) <= 8:
        return int(value)
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max(0, int((retry_at - datetime.now(UTC)).total_seconds()))
    except (ValueError, OverflowError):
        return None


def _download(client, settings, endpoint, auth, params):
    try:
        options = {"params" if settings.method == "GET" else "json": params}
        headers = {settings.token_header: auth, "Accept": "application/json"}
        with client.stream(
            settings.method, endpoint, headers=headers, follow_redirects=False, **options,
        ) as response:
            status = response.status_code
            if not 200 <= status < 300:
                raise TransportError(
                    f"Brand Analytics отклонил запрос ({status})",
                    status_code=status,
                    retry_after=_retry_after(response.headers.get("Retry-After")),
                    retryable=status in {408, 425, 429, 500, 502, 503, 504},
                )
            raw = bytearray()
            for chunk in response.iter_bytes(chunk_size=64 * 1024):
                if len(raw) + len(chunk) > _MAX_PAGE_BYTES:
                    raise TransportError("Страница Brand Analytics превышает допустимый размер")
                raw.extend(chunk)
            return raw
    except httpx.RequestError:
        raise TransportError("Не удалось получить ответ Brand Analytics", retryable=True) from None


def fetch_page(settings, *, topic_id, since, cursor=None, until=None, client=None):
    """Read one page. The caller owns checkpointing and the request budget."""
    if topic_id not in settings.topic_ids:
        raise TransportError("Тема отсутствует в настройках Brand Analytics")
    fields = settings.request_fields
    since_value = _time_parameter(since, settings.date_format)
    until_value = _time_parameter(until, settings.date_format) if until is not None else None
    if until is not None and until <= since:
        raise TransportError("Некорректный интервал запроса Brand Analytics")
    params = dict(settings.static_params)
    for parameter, value in (
        (fields.topic_id, topic_id if "{topic_id}" not in settings.endpoint else None),
        (fields.since, since_value),
        (fields.until, until_value),
        (fields.cursor, cursor),
        (fields.limit, settings.page_size),
    ):
        if parameter is not None and value is not None:
            params[parameter] = value
    endpoint = settings.endpoint.replace("{topic_id}", quote(topic_id, safe=""))
    token = settings.token.get_secret_value()
    auth = f"{settings.token_scheme} {token}" if settings.token_scheme else token
    if client is None:
        with httpx.Client(timeout=20, trust_env=False, verify=True) as own_client:
            raw = _download(own_client, settings, endpoint, auth, params)
    else:
        raw = _download(client, settings, endpoint, auth, params)
    payload = _json(raw)
    rows = _message_rows(payload, settings.messages_path)
    if len(rows) > 500:
        raise TransportError("Слишком много сообщений на одной странице Brand Analytics")
    next_cursor = _next_cursor(payload, settings, len(rows))
    if next_cursor is not None and next_cursor == cursor:
        raise TransportError("Brand Analytics вернул уже обработанный курсор")
    return MessagePage(
        _normalize_rows(rows, settings.field_map, ZoneInfo(settings.source_timezone)),
        next_cursor,
        len(rows),
    )


def read_export(raw, *, field_map=None, messages_path=None, source_timezone="Europe/Moscow"):
    """Parse a message-level JSON or CSV export with original links and texts."""
    if not raw or len(raw) > _MAX_EXPORT_BYTES:
        raise TransportError("Файл выгрузки Brand Analytics пустой или больше 32 МБ")
    stripped = raw.removeprefix(b"\xef\xbb\xbf").lstrip()
    if stripped.startswith((b"{", b"[")):
        rows = _message_rows(_json(stripped), messages_path)
    else:
        try:
            decoded = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                decoded = raw.decode("cp1251")
            except UnicodeDecodeError:
                raise TransportError("Не удалось прочитать кодировку CSV Brand Analytics") from None
        try:
            delimiter = csv.Sniffer().sniff(decoded[:4096], delimiters=";,\t").delimiter
        except csv.Error:
            delimiter = ";" if ";" in decoded.partition("\n")[0] else ","
        try:
            reader = csv.DictReader(io.StringIO(decoded, newline=""), delimiter=delimiter)
            if not reader.fieldnames or len(reader.fieldnames) < 2:
                raise TransportError("Для импорта нужны сообщения с исходной ссылкой и текстом")
            rows = []
            for row in reader:
                if len(rows) >= _MAX_EXPORT_ROWS:
                    raise TransportError("В выгрузке Brand Analytics больше 50 000 строк")
                rows.append(row)
        except csv.Error:
            raise TransportError("Некорректный CSV Brand Analytics") from None
    if len(rows) > _MAX_EXPORT_ROWS:
        raise TransportError("В выгрузке Brand Analytics больше 50 000 строк")
    return _normalize_rows(rows, field_map or {}, ZoneInfo(source_timezone))
