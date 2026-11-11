"""Idempotent Google Sheets projection. Only app-owned tabs are written."""

import json
import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import service_account

from radar.web import stoplist
from radar.web.chats import aggregates
from radar.web.configuration import configuration, write_secrets
from radar.web.store import STATUSES, now

LOCK = threading.Lock()
TABS = ["Реестр", "Неделя", "Девелоперы", "Чаты", "Воронка", "Справка", "Стоп-лист", "Заметки"]
PREFIX = "Мегаполис · "
META = "megapolis_managed_v1"


def credential_info(root):
    cfg = configuration(root)
    if cfg.get("GOOGLE_SA_CREDENTIALS"):
        raw = cfg["GOOGLE_SA_CREDENTIALS"]
    elif cfg.get("GOOGLE_SA_JSON"):
        path = Path(cfg["GOOGLE_SA_JSON"]).expanduser()
        if not path.is_file() or path.stat().st_size > 30000:
            raise ValueError("Файл сервисного аккаунта не найден или слишком велик")
        raw = path.read_text()
    else:
        raise ValueError("Загрузите JSON сервисного аккаунта в подключениях")
    return validate_credentials(raw)


def validate_credentials(raw):
    try:
        info = json.loads(raw)
        if (
            info.get("type") != "service_account"
            or info.get("universe_domain", "googleapis.com") != "googleapis.com"
            or info.get("token_uri") != "https://oauth2.googleapis.com/token"
        ):
            raise ValueError()
        if not info.get("client_email", "").endswith(".gserviceaccount.com"):
            raise ValueError()
        if not info.get("private_key", "").startswith("-----BEGIN PRIVATE KEY-----"):
            raise ValueError()
        return info
    except (ValueError, AttributeError):
        raise ValueError("Нужен JSON сервисного аккаунта Google с официальным token_uri") from None


def save_credentials(root, raw):
    info = validate_credentials(raw)
    write_secrets(root, {"GOOGLE_SA_CREDENTIALS": json.dumps(info)})
    return {"saved": True, "client_email": info["client_email"]}


def access_token(root):
    try:
        credentials = service_account.Credentials.from_service_account_info(
            credential_info(root), scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        request = Request()
        credentials.refresh(lambda **kwargs: request(**{**kwargs, "timeout": 25}))
        return credentials.token
    except Exception:
        raise ValueError(
            "Не удалось авторизовать сервисный аккаунт Google. Проверьте ключ и доступ"
        ) from None


def local_time(value):
    return (
        datetime.fromisoformat(value)
        .astimezone(ZoneInfo("Europe/Moscow"))
        .strftime("%d.%m.%Y %H:%M")
    )


def tables(store):
    records = [
        r
        for dataset in ("web", "imported")
        for r in store.records(dataset)
        if r.get("granularity") != "developer"
    ]
    exclusions = stoplist.entries(store)
    registry = [
        [
            "ID",
            "ЖК",
            "Корпус",
            "Девелопер",
            "Регион",
            "Статус",
            "Сигнал",
            "Проверка",
            "Стоп-лист",
            "Источники",
            "Обновлено МСК",
        ]
    ]
    funnel = [["ID", "ЖК", "Девелопер", "Статус", "Комментарий", "Причина отказа", "Обновлено МСК"]]
    developers = {}
    chats = [
        [
            "ID объекта",
            "ЖК",
            "Чатов",
            "Сообщений за 30 дней",
            "Авторов",
            "Негативных среди размеченных, %",
            "Моделью размечено",
            "Метод",
        ]
    ]
    for r in records:
        registry.append(
            [
                r["id"],
                r["name"],
                r.get("building") or "",
                r["developer"],
                r["region"],
                STATUSES[r["status"]],
                r["summary"],
                r["check"],
                "Да" if stoplist.match(r, exclusions) else "",
                "\n".join(r["urls"]),
                local_time(r["updated_at"]),
            ]
        )
        developers.setdefault(r["developer"], set()).add(r["id"])
        if r["status"] not in {"new", "review"}:
            funnel.append(
                [
                    r["id"],
                    r["name"],
                    r["developer"],
                    STATUSES[r["status"]],
                    r["note"],
                    r.get("refusal_reason", ""),
                    local_time(r["updated_at"]),
                ]
            )
        stats = aggregates(store, r["id"])
        if stats["chats"]:
            chats.append(
                [
                    r["id"],
                    r["name"],
                    len(stats["chats"]),
                    stats["messages_30d"],
                    stats["authors_30d"],
                    stats["negative_share"] if stats["negative_share"] is not None else "",
                    stats["model_labelled"],
                    stats["method"],
                ]
            )
    with store.connect() as db:
        events = [
            dict(r)
            for r in db.execute(
                "SELECT e.*,json_extract(r.payload,'$.name') AS name "
                "FROM events e JOIN records r ON r.id=e.record_id WHERE "
                "r.dataset!='demo' AND e.created_at>=? ORDER BY e.id DESC LIMIT 10000",
                ((datetime.now(UTC) - timedelta(days=7)).isoformat(),),
            )
        ]
    return {
        "Реестр": registry,
        "Воронка": funnel,
        "Чаты": chats,
        "Девелоперы": [["Девелопер", "Объектов в локальном реестре"]]
        + [[d, len(ids)] for d, ids in sorted(developers.items())],
        "Неделя": [["ID события", "Объект", "Действие", "Описание", "Дата МСК"]]
        + [
            [e["id"], e["name"], e["action"], e["detail"], local_time(e["created_at"])]
            for e in events
        ],
        "Справка": [
            ["Параметр", "Значение"],
            ["Обновлено МСК", local_time(now())],
            ["Источник", "Мегаполис Интеледженс. Системные листы обновляются целиком."],
            ["Заметки", "Свободные заметки храните на отдельном листе Заметки."],
            [
                "Стоп-лист",
                "Тип: record или developer; значение: ID карточки или точное имя застройщика.",
            ],
            [
                "Проверка",
                "Утверждения источников требуют проверки; нет подтверждения полноты охвата.",
            ],
        ],
        "Стоп-лист": [["Тип", "Значение", "Причина: client/partner/refused/other", "Комментарий"]],
        "Заметки": [["Дата", "Объект", "Заметка"]],
    }


def initialize(store):
    with store.connect() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS sheets_syncs(id INTEGER PRIMARY KEY,status "
            "TEXT,payload TEXT,created_at TEXT)"
        )


def status(store):
    initialize(store)
    cfg = configuration(store.root)
    with store.connect() as db:
        history = [
            dict(r) for r in db.execute("SELECT * FROM sheets_syncs ORDER BY id DESC LIMIT 10")
        ]
    return {
        "configured": bool(
            cfg.get("SHEET_ID") and (cfg.get("GOOGLE_SA_JSON") or cfg.get("GOOGLE_SA_CREDENTIALS"))
        ),
        "history": history,
    }


def sync(store, client=None, token_provider=access_token):
    if not LOCK.acquire(blocking=False):
        raise ValueError("Синхронизация таблицы уже выполняется")
    owns = client is None
    try:
        initialize(store)
        cfg = configuration(store.root)
        sheet_id = cfg.get("SHEET_ID", "")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{10,150}", sheet_id):
            raise ValueError("Сохраните корректный ID Google Таблицы")
        token = token_provider(store.root)
        client = client or httpx.Client(timeout=40)
        base = "https://sheets.googleapis.com/v4/spreadsheets/" + sheet_id
        headers = {"Authorization": "Bearer " + token}

        def call(method, suffix="", body=None):
            try:
                response = client.request(method, base + suffix, headers=headers, json=body)
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError):
                raise ValueError(
                    "Google Sheets не подтвердил операцию. Проверьте доступ и "
                    "повторите: записи идемпотентны"
                ) from None

        book = call("GET", "?fields=sheets.properties")
        metadata = call(
            "POST",
            "/developerMetadata:search",
            {"dataFilters": [{"developerMetadataLookup": {"metadataKey": META}}]},
        )
        owned = {
            m["developerMetadata"]["metadataValue"]: m["developerMetadata"]["location"]["sheetId"]
            for m in metadata.get("matchedDeveloperMetadata", [])
        }
        existing = {s["properties"]["title"]: s["properties"] for s in book.get("sheets", [])}
        next_id = max([s["sheetId"] for s in existing.values()] + [0]) + 1
        requests, ids, new_tabs = [], {}, set()
        for tab in TABS:
            title = PREFIX + tab
            if title in existing:
                ident = existing[title]["sheetId"]
                if owned.get(tab) != ident:
                    raise ValueError(
                        "Лист «"
                        + title
                        + "» уже существует и не принадлежит приложению. Переименуйте его"
                    )
            else:
                ident, next_id = next_id, next_id + 1
                new_tabs.add(tab)
                requests.extend(
                    [
                        {
                            "addSheet": {
                                "properties": {
                                    "sheetId": ident,
                                    "title": title,
                                    "gridProperties": {
                                        "rowCount": 1000,
                                        "columnCount": 16,
                                        "frozenRowCount": 1,
                                    },
                                }
                            }
                        },
                        {
                            "createDeveloperMetadata": {
                                "developerMetadata": {
                                    "metadataKey": META,
                                    "metadataValue": tab,
                                    "location": {"sheetId": ident},
                                    "visibility": "DOCUMENT",
                                }
                            }
                        },
                        {
                            "repeatCell": {
                                "range": {"sheetId": ident, "startRowIndex": 0, "endRowIndex": 1},
                                "cell": {
                                    "userEnteredFormat": {
                                        "backgroundColor": {
                                            "red": 0.09,
                                            "green": 0.24,
                                            "blue": 0.33,
                                        },
                                        "textFormat": {
                                            "bold": True,
                                            "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                                        },
                                    }
                                },
                                "fields": "userEnteredFormat",
                            }
                        },
                    ]
                )
                if tab not in {"Стоп-лист", "Заметки"}:
                    requests.append(
                        {
                            "addProtectedRange": {
                                "protectedRange": {
                                    "range": {"sheetId": ident},
                                    "description": "Системный лист Мегаполиса",
                                    "warningOnly": True,
                                }
                            }
                        }
                    )
            ids[tab] = ident
        unresolved = []
        if "Стоп-лист" not in new_tabs:
            values = call("GET", "/values/" + quote("'" + PREFIX + "Стоп-лист'!A2:D", safe=""))
            if len(values.get("values", [])) > 10000:
                raise ValueError("В стоп-листе больше 10000 строк: требуется разбор вручную")
            source = "sheets:" + sheet_id
            desired = set()
            for index, cells in enumerate(values.get("values", []), 2):
                if not any(str(c).strip() for c in cells):
                    continue
                try:
                    entry = stoplist.StopEntry(
                        kind=cells[0],
                        value=cells[1],
                        reason=cells[2] if len(cells) > 2 and cells[2] else "other",
                        note=cells[3] if len(cells) > 3 else "",
                    )
                    key = (entry.kind, stoplist.normalized(entry.value))
                    desired.add(key)
                    old = next(
                        (r for r in stoplist.entries(store) if (r["kind"], r["normalized"]) == key),
                        None,
                    )
                    if not old or old["actor"] == source:
                        stoplist.add(store, entry, source)
                except (ValueError, IndexError, KeyError):
                    unresolved.append(index)
            # Do not remove prior exclusions while any row is ambiguous or invalid.
            if not unresolved:
                for old in stoplist.entries(store):
                    if old["actor"] == source and (old["kind"], old["normalized"]) not in desired:
                        stoplist.remove(store, old["id"])
        for tab, rows in tables(store).items():
            if tab in {"Стоп-лист", "Заметки"} and tab not in new_tabs:
                continue
            ident = ids[tab]
            width = max(len(row) for row in rows)
            old_size = (
                existing.get(PREFIX + tab, {}).get("gridProperties", {}).get("rowCount", 1000)
            )
            if len(rows) > old_size:
                requests.append(
                    {
                        "updateSheetProperties": {
                            "properties": {
                                "sheetId": ident,
                                "gridProperties": {"rowCount": len(rows) + 100},
                            },
                            "fields": "gridProperties.rowCount",
                        }
                    }
                )
            encoded = [
                {
                    "values": [
                        {
                            "userEnteredValue": {"numberValue": cell}
                            if isinstance(cell, (int, float))
                            else {"stringValue": str(cell)[:45000]}
                        }
                        for cell in row
                    ]
                }
                for row in rows
            ]
            requests.append(
                {
                    "updateCells": {
                        "range": {
                            "sheetId": ident,
                            "startRowIndex": 0,
                            "startColumnIndex": 0,
                            "endColumnIndex": width,
                        },
                        "rows": encoded,
                        "fields": "userEnteredValue",
                    }
                }
            )
            if tab not in {"Стоп-лист", "Заметки"}:
                requests.append(
                    {
                        "setBasicFilter": {
                            "filter": {
                                "range": {
                                    "sheetId": ident,
                                    "startRowIndex": 0,
                                    "endRowIndex": len(rows),
                                    "startColumnIndex": 0,
                                    "endColumnIndex": width,
                                }
                            }
                        }
                    }
                )
        call("POST", ":batchUpdate", {"requests": requests})
        result = {
            "tabs": len(TABS),
            "unresolved_stoplist_rows": unresolved,
            "url": "https://docs.google.com/spreadsheets/d/" + sheet_id,
        }
        with store.connect() as db:
            db.execute(
                "INSERT INTO sheets_syncs(status,payload,created_at) VALUES('completed',?,?)",
                (json.dumps(result), now()),
            )
        return result
    except ValueError as exc:
        with store.connect() as db:
            db.execute(
                "INSERT INTO sheets_syncs(status,payload,created_at) VALUES('failed',?,?)",
                (json.dumps({"error": str(exc)}), now()),
            )
        raise
    finally:
        if owns and client is not None:
            client.close()
        LOCK.release()
