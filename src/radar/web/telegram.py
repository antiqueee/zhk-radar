"""Read-only public Telegram source adapter; credentials stay in .env."""

import asyncio
import re
import threading
import time
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field, field_validator
from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telethon.tl.types import Channel

from radar.web.chats import ChatImport, ingest_chat
from radar.web.configuration import configuration, write_secrets
from radar.web.store import now


class TelegramLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone: str = Field(pattern=r"^\+[1-9][0-9]{7,14}$")


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


def factory(cfg, session):
    return TelegramClient(
        StringSession(session),
        int(cfg["TG_API_ID"]),
        cfg["TG_API_HASH"],
        receive_updates=False,
        request_retries=0,
        connection_retries=1,
        flood_sleep_threshold=0,
        timeout=15,
    )


class TelegramReader:
    def __init__(self, store, salt, client_factory=factory):
        self.store, self.salt, self.factory = store, salt, client_factory
        self.lock = threading.Lock()
        self.pending = None
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS telegram_links(
                record_id TEXT NOT NULL, url TEXT NOT NULL, peer_id TEXT, title TEXT,
                cursor INTEGER NOT NULL DEFAULT 0, synced_at TEXT, status TEXT NOT NULL,
                PRIMARY KEY(record_id,url))""")

    def status(self):
        cfg = configuration(self.store.root)
        return {
            "configured": bool(cfg.get("TG_API_ID") and cfg.get("TG_API_HASH")),
            "session_saved": bool(cfg.get("TG_SESSION")),
            "pending_code": bool(self.pending and self.pending["expires"] > time.time()),
            "verified": False,
        }

    def invoke(self, action, session=None):
        with self.lock:
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
            except (errors.RPCError, OSError, TimeoutError):
                raise ValueError(
                    "Telegram недоступен или отказал в доступе. Проверьте подключение"
                ) from None

    def login(self, phone):
        async def action(client):
            if self.pending and self.pending["expires"] > time.time():
                raise ValueError("Код уже запрошен. Введите его или отмените вход")
            result = await client.send_code_request(phone)
            self.pending = {
                "phone": phone,
                "hash": result.phone_code_hash,
                "session": client.session.save(),
                "expires": time.time() + 300,
            }
            return {"status": "code_sent", "expires_in": 300}

        return self.invoke(action, "")

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
        return self.links(record_id)

    def inspect(self, url):
        url = TelegramLink(url=url).url

        async def action(client):
            from telethon.tl.functions.channels import GetFullChannelRequest

            entity = await client.get_entity(url.rsplit("/", 1)[1])
            if not isinstance(entity, Channel) or not entity.username:
                raise ValueError("Это не публичная группа или канал")
            full = await client(GetFullChannelRequest(entity))
            return {
                "url": url,
                "title": entity.title,
                "description": (full.full_chat.about or "")[:1000],
                "participants": full.full_chat.participants_count,
                "group": bool(entity.megagroup),
                "public": True,
                "note": "Адрес и описание проверены; соответствие ЖК подтверждает аналитик",
            }

        return self.invoke(action)

    def sync(self, record_id, url, limit=1000):
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
            since = datetime.now(UTC) - timedelta(days=90)
            messages, cursor, seen = [], link["cursor"], 0
            kwargs = dict(limit=limit + 1, min_id=cursor, reverse=True)
            if not cursor:
                kwargs["offset_date"] = since
            async for message in client.iter_messages(entity, **kwargs):
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
                        cursor,
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
                "note": "Без разметки тем и тональности; повторный сбор не дублирует сообщения",
            }

        return self.invoke(action)
