"""Read-only public Telegram source adapter; credentials stay in .env."""

import asyncio
import json
import math
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator
from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telethon.tl.functions.auth import ResendCodeRequest
from telethon.tl.types import Channel

from radar.web.chats import ChatImport, ingest_chat
from radar.web.configuration import configuration, write_secrets
from radar.web.store import now


class TelegramLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone: str = Field(pattern=r"^\+[1-9][0-9]{7,14}$")

    @field_validator("phone", mode="before")
    @classmethod
    def normalize_phone(cls, value):
        return re.sub(r"[\s()-]", "", value) if isinstance(value, str) else value


class TelegramCode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(default="", pattern=r"^[0-9]{0,8}$")
    password: str = Field(default="", max_length=200)


class TelegramLink(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(max_length=150)

    @field_validator("url")
    @classmethod
    def public_link(cls, value):
        match = re.fullmatch(r"https://t\.me/(?:s/)?([A-Za-z][A-Za-z0-9_]{3,31})/?", value.strip())
        if not match or match[1].lower() in {"joinchat", "share", "proxy", "socks"}:
            raise ValueError("Нужна открытая ссылка https://t.me/имя_чата, без приглашения")
        return "https://t.me/" + match[1].lower()


def proxy_options(value):
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (
            parsed.scheme not in {"http", "socks5"}
            or not parsed.hostname
            or not port
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("Некорректная настройка маршрута Telegram") from None
    return {
        "proxy_type": parsed.scheme,
        "addr": parsed.hostname,
        "port": port,
        "rdns": True,
        "username": unquote(parsed.username) if parsed.username else None,
        "password": unquote(parsed.password) if parsed.password else None,
    }


def factory(cfg, session):
    return TelegramClient(
        StringSession(session),
        int(cfg["TG_API_ID"]),
        cfg["TG_API_HASH"],
        receive_updates=bool(cfg.get("_receive_updates", False)),
        request_retries=0,
        connection_retries=1,
        flood_sleep_threshold=0,
        timeout=15,
        proxy=proxy_options(cfg.get("RADAR_TELEGRAM_PROXY_URL")),
    )


class TelegramReader:
    def __init__(self, store, salt, client_factory=factory):
        self.store, self.salt, self.factory = store, salt, client_factory
        self.lock = threading.Lock()
        self.pending = None
        self.qr = None
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS telegram_links(
                record_id TEXT NOT NULL, url TEXT NOT NULL, peer_id TEXT, title TEXT,
                cursor INTEGER NOT NULL DEFAULT 0, synced_at TEXT, status TEXT NOT NULL,
                PRIMARY KEY(record_id,url))""")

    def status(self):
        cfg = configuration(self.store.root)
        pending = self.pending
        active = bool(pending and pending["expires"] > time.time())
        return {
            "configured": bool(cfg.get("TG_API_ID") and cfg.get("TG_API_HASH")),
            "session_saved": bool(cfg.get("TG_SESSION")),
            "pending_code": active,
            "login": self.login_status(pending) if active else None,
            "verified": False,
        }

    def invoke(self, action, session=None):
        with self.lock:
            if self.qr and self.qr.active:
                raise ValueError(
                    "Завершите вход по QR-коду или отмените его перед входом по номеру."
                )
            cfg = configuration(self.store.root)
            if not cfg.get("TG_API_ID", "").isdigit() or not cfg.get("TG_API_HASH"):
                raise ValueError("Сначала сохраните API ID и API Hash Telegram")

            async def execute():
                client = self.factory(
                    cfg, session if session is not None else cfg.get("TG_SESSION", "")
                )
                try:
                    await client.connect()
                    return await action(client)
                finally:
                    await client.disconnect()

            try:
                return asyncio.run(asyncio.wait_for(execute(), timeout=90))
            except errors.FloodWaitError as exc:
                raise ValueError(f"Telegram просит подождать {exc.seconds} секунд") from None
            except (errors.PhoneCodeInvalidError, errors.PhoneCodeExpiredError):
                raise ValueError(
                    "Код неверен или истёк. Запросите новый код при необходимости"
                ) from None
            except errors.PasswordHashInvalidError:
                raise ValueError("Неверный пароль двухэтапной проверки") from None
            except errors.ApiIdInvalidError:
                raise ValueError(
                    "Telegram отклонил API ID / API Hash. Проверьте пару ключей."
                ) from None
            except errors.PhoneNumberInvalidError:
                raise ValueError(
                    "Telegram не принял номер. Укажите его с кодом страны: +7…"
                ) from None
            except errors.PhoneNumberBannedError:
                raise ValueError("Telegram сообщает, что этот номер заблокирован.") from None
            except errors.RPCError as exc:
                # Only the exception class is exposed; never the request or its arguments.
                raise ValueError(f"Telegram отклонил запрос: {type(exc).__name__}") from None
            except (OSError, TimeoutError):
                raise ValueError(
                    "Не удалось связаться с Telegram. Проверьте интернет и доступ к Telegram."
                ) from None

    @staticmethod
    def login_status(pending):
        delivery = pending.get("delivery", "unknown")
        messages = {
            "SentCodeTypeApp": (
                "Откройте служебный чат «Telegram» на устройстве, где уже "
                "выполнен вход. Telegram выбрал доставку кода в приложение, не SMS."
            ),
            "SentCodeTypeSms": "Telegram выбрал SMS. Проверьте сообщения на телефоне.",
            "SentCodeTypeCall": "Telegram выбрал телефонный звонок с кодом.",
            "SentCodeTypeFlashCall": (
                "Telegram выбрал звонок: код определяется по номеру звонящего."
            ),
            "SentCodeTypeMissedCall": (
                "Telegram выбрал пропущенный звонок: код — последние цифры номера звонящего."
            ),
            "SentCodeTypeEmailCode": (
                "Telegram выбрал доставку кода на почту, привязанную к входу в Telegram."
            ),
        }
        delay = max(0, math.ceil(pending.get("resend_at", 0) - time.time()))
        return {
            "status": "code_sent",
            "delivery": delivery,
            "message": messages.get(
                delivery,
                (
                    "Telegram ответил на запрос. Проверьте служебный чат "
                    "«Telegram». Способ доставки не распознан приложением."
                ),
            ),
            "expires_in": max(0, math.ceil(pending["expires"] - time.time())),
            "resend_available": bool(pending.get("next_type")),
            "resend_in": delay,
            "password_needed": pending.get("password_needed", False),
        }

    def remember_code(self, client, phone, result):
        delivery = type(getattr(result, "type", None)).__name__
        if not getattr(result, "phone_code_hash", None):
            raise ValueError(
                "Telegram не выдал запрос кода для входа. Обновите "
                "официальный Telegram и повторите вход."
            )
        if delivery in {
            "SentCodeTypeSetUpEmailRequired",
            "SentCodeTypeFirebaseSms",
            "SentCodeTypeSmsWord",
            "SentCodeTypeSmsPhrase",
        }:
            raise ValueError(
                "Telegram запросил способ входа, который здесь пока не "
                "поддерживается. Выполните вход в официальном приложении Telegram."
            )
        self.pending = {
            "phone": phone,
            "hash": result.phone_code_hash,
            "session": client.session.save(),
            "expires": time.time() + 300,
            "delivery": delivery,
            "next_type": type(result.next_type).__name__
            if getattr(result, "next_type", None)
            else None,
            "resend_at": time.time() + max(0, getattr(result, "timeout", None) or 0),
        }
        return self.login_status(self.pending)

    def login(self, phone):
        async def action(client):
            if self.pending and self.pending["expires"] > time.time():
                raise ValueError("Код уже запрошен. Введите его или отмените вход")
            result = await client.send_code_request(phone)
            return self.remember_code(client, phone, result)

        return self.invoke(action, "")

    def resend(self):
        pending = self.pending
        if not pending or pending["expires"] <= time.time():
            raise ValueError("Запрос входа истёк. Запросите новый код.")

        async def action(client):
            if self.pending is not pending or pending["expires"] <= time.time():
                raise ValueError("Запрос входа уже завершён или отменён")
            status = self.login_status(pending)
            if pending.get("password_needed"):
                raise ValueError("Код уже принят. Введите пароль двухэтапной проверки.")
            if not status["resend_available"]:
                raise ValueError(
                    "Telegram не предложил другой способ доставки. Проверьте "
                    "служебный чат «Telegram»."
                )
            if status["resend_in"]:
                raise ValueError(f"Повторная отправка доступна через {status['resend_in']} сек.")
            result = await client(ResendCodeRequest(pending["phone"], pending["hash"]))
            return self.remember_code(client, pending["phone"], result)

        return self.invoke(action, pending["session"])

    def confirm(self, data):
        pending = self.pending
        if not pending or pending["expires"] <= time.time():
            self.pending = None
            raise ValueError("Вход истёк. Запросите новый код")

        async def action(client):
            if self.pending is not pending or pending["expires"] <= time.time():
                raise ValueError("Запрос входа уже завершён или отменён")
            try:
                if data.password:
                    await client.sign_in(password=data.password)
                else:
                    await client.sign_in(
                        phone=pending["phone"], code=data.code, phone_code_hash=pending["hash"]
                    )
            except errors.SessionPasswordNeededError:
                self.pending["session"] = client.session.save()
                self.pending["password_needed"] = True
                return {"status": "password_needed"}
            if not await client.is_user_authorized():
                raise ValueError("Авторизация не завершена")
            write_secrets(self.store.root, {"TG_SESSION": client.session.save()})
            self.pending = None
            return {"status": "authorized"}

        return self.invoke(action, pending["session"])

    def disconnect(self):
        # Forget locally. Remote revocation can be performed in Telegram Devices.
        with self.lock:
            self.pending = None
            write_secrets(self.store.root, {"TG_SESSION": ""})
        return {"status": "disconnected"}

    def links(self, record_id=None):
        with self.store.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM telegram_links" + (" WHERE record_id=?" if record_id else ""),
                    (record_id,) if record_id else (),
                )
            ]

    def add(self, record_id, data):
        self.store.get(record_id)
        with self.store.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO telegram_links(record_id,url,status) VALUES(?,?,'added')",
                (record_id, data.url),
            )
        return self.links(record_id)

    def remove(self, record_id, data):
        with self.store.connect() as db:
            db.execute(
                "DELETE FROM telegram_links WHERE record_id=? AND url=?", (record_id, data.url)
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS chat_candidates(record_id TEXT,url TEXT,"
                "payload TEXT,status TEXT,PRIMARY KEY(record_id,url))"
            )
            db.execute(
                "INSERT INTO chat_candidates VALUES(?,?,?,'rejected') "
                "ON CONFLICT(record_id,url) DO UPDATE SET status='rejected'",
                (
                    record_id,
                    data.url,
                    json.dumps(
                        {"url": data.url, "title": data.url, "note": "Ссылка удалена оператором"}
                    ),
                ),
            )
        return self.links(record_id)

    def inspect(self, url):
        url = TelegramLink(url=url).url

        async def action(client):
            from telethon.tl.functions.channels import GetFullChannelRequest

            entity = await client.get_entity(url.rsplit("/", 1)[1])
            usernames = {getattr(entity, "username", "") or ""}
            usernames.update(u.username for u in getattr(entity, "usernames", []) or [] if u.active)
            if not isinstance(entity, Channel) or url.rsplit("/", 1)[1] not in {
                u.lower() for u in usernames
            }:
                raise ValueError("Это не публичная группа или канал")
            full = await client(GetFullChannelRequest(entity))
            return {
                "url": url,
                "peer_id": str(entity.id),
                "title": entity.title,
                "description": (full.full_chat.about or "")[:1000],
                "participants": full.full_chat.participants_count,
                "group": bool(entity.megagroup),
                "public": True,
                "note": "Адрес и описание проверены; соответствие ЖК подтверждает аналитик",
            }

        return self.invoke(action)

    def sync(self, record_id, url, limit=1000, *, recent=False):
        if not configuration(self.store.root).get("TG_SESSION"):
            raise ValueError("Авторизуйте читатель Telegram в подключениях")
        link = next((r for r in self.links(record_id) if r["url"] == url), None)
        if not link:
            raise ValueError("Сначала добавьте ссылку в карточку")

        async def action(client):
            if not await client.is_user_authorized():
                raise ValueError("Сессия Telegram недействительна. Войдите заново")
            entity = await client.get_entity(url.rsplit("/", 1)[1])
            usernames = {getattr(entity, "username", "") or ""}
            usernames.update(u.username for u in getattr(entity, "usernames", []) or [] if u.active)
            if not isinstance(entity, Channel) or url.rsplit("/", 1)[1] not in {
                u.lower() for u in usernames
            }:
                raise ValueError("Источник не является открытым каналом или супергруппой")
            if link["peer_id"] and link["peer_id"] != str(entity.id):
                raise ValueError(
                    "Ссылка теперь ведёт на другой чат. Удалите её и проверьте источник"
                )
            since = datetime.now(UTC) - timedelta(days=30)
            messages, cursor, seen = [], link["cursor"], 0
            kwargs = dict(limit=limit + 1, min_id=cursor, reverse=not recent)
            if not recent:
                kwargs["offset_date"] = since
            async for message in client.iter_messages(entity, **kwargs):
                if recent and message.date < since:
                    break
                seen += 1
                if seen > limit:
                    break
                cursor = max(cursor, message.id)
                if message.date < since or not message.message:
                    continue
                messages.append(
                    {
                        "id": str(message.id),
                        "author_id": str(message.sender_id or entity.id),
                        "sent_at": message.date,
                        "text": message.message[:10000],
                    }
                )
            batch = ChatImport(
                record_id=record_id,
                chat_id=str(entity.id),
                title=entity.title,
                access="public",
                messages=messages,
            )
            result = ingest_chat(self.store, batch, self.salt())
            with self.store.connect() as db:
                db.execute(
                    "UPDATE telegram_links SET peer_id=?,title=?,cursor=?,synced_at=?,status=? "
                    "WHERE record_id=? AND url=?",
                    (
                        str(entity.id),
                        entity.title,
                        link["cursor"] if recent and seen > limit else cursor,
                        now(),
                        "partial" if seen > limit else "synced",
                        record_id,
                        url,
                    ),
                )
            return {
                "ingest": result,
                "partial": seen > limit,
                "read": len(messages),
                "period_days": 30,
                "recent_sample": recent and seen > limit,
                "note": "Без разметки тем и тональности; повторный сбор не дублирует сообщения",
            }

        return self.invoke(action)
