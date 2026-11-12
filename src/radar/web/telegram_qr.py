"""Short-lived QR authorization. Tokens stay in memory and in the admin UI only."""

import asyncio
import base64
import time

from pydantic import BaseModel, ConfigDict, Field
from reportlab.graphics import renderSVG
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from telethon import errors

from radar.web.configuration import configuration, write_secrets


class QRPassword(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=1, max_length=200)


def qr_image(url):
    widget = QrCodeWidget(url, barWidth=280, barHeight=280, barBorder=4)
    drawing = Drawing(280, 280)
    drawing.add(widget)
    svg = renderSVG.drawToString(drawing).encode()
    return "data:image/svg+xml;base64," + base64.b64encode(svg).decode()


def error_message(exc):
    if isinstance(exc, errors.FloodWaitError):
        return f"Telegram просит подождать {exc.seconds} секунд."
    if isinstance(exc, errors.ApiIdInvalidError):
        return "Telegram отклонил API ID / API Hash. Проверьте пару ключей."
    if isinstance(exc, (OSError, TimeoutError)):
        return "Не удалось связаться с Telegram. Проверьте подключение и повторите вход."
    # Never stringify RPC exceptions: their request can contain authentication data.
    return f"Не удалось завершить вход в Telegram ({type(exc).__name__}). Повторите вход."


class TelegramQR:
    def __init__(self, reader):
        self.reader = reader
        self.lock = asyncio.Lock()
        self.task = None
        self.state = {"status": "idle", "message": "Нажмите «Войти по QR-коду»."}
        self.password_session = None
        self.deadline = 0

    @property
    def active(self):
        return self.state["status"] in {"starting", "waiting", "password_needed"}

    def status(self):
        if self.state["status"] == "password_needed" and time.time() >= self.deadline:
            self.password_session = None
            self.state = {
                "status": "expired",
                "message": "Время входа истекло. Создайте новый QR-код.",
            }
        return dict(self.state)

    async def start(self):
        async with self.lock:
            self.status()
            if self.active:
                return self.status()
            cfg = configuration(self.reader.store.root)
            if not cfg.get("TG_API_ID", "").isdigit() or not cfg.get("TG_API_HASH"):
                raise ValueError("Сначала сохраните API ID и API Hash Telegram")
            if cfg.get("TG_SESSION"):
                self.state = {
                    "status": "authorized",
                    "message": "Сессия уже сохранена. Можно читать чаты.",
                }
                return self.status()
            self.state = {"status": "starting", "message": "Подключаемся к Telegram…"}
            self.deadline = time.time() + 300
            self.task = asyncio.create_task(self.run(cfg))
            return self.status()

    def clear_code_login(self):
        # Wait for an already running code request before switching authentication modes.
        with self.reader.lock:
            self.reader.pending = None

    async def run(self, cfg):
        client = None
        try:
            await asyncio.to_thread(self.clear_code_login)
            client = self.reader.factory({**cfg, "_receive_updates": True}, "")
            await asyncio.wait_for(client.connect(), 30)
            # The history reader disables updates. QR login MUST receive UpdateLoginToken.
            while time.time() < self.deadline:
                qr = await asyncio.wait_for(client.qr_login(), 20)
                ttl = min(self.deadline - time.time(), qr.expires.timestamp() - time.time())
                if ttl <= 0:
                    raise TimeoutError()
                waiter = asyncio.create_task(qr.wait(timeout=ttl))
                # Install the update listener before publishing the scannable token.
                try:
                    await asyncio.sleep(0)
                    self.state = {
                        "status": "waiting",
                        "message": (
                            "Telegram на телефоне → Настройки → Устройства → Подключить "
                            "устройство. Отсканируйте QR-код и подтвердите вход."
                        ),
                        "image": qr_image(qr.url),
                        "expires_at": qr.expires.isoformat(),
                    }
                    await waiter
                except TimeoutError:
                    continue  # Telegram tokens expire quickly; refresh without user clicks.
                except errors.SessionPasswordNeededError:
                    self.password_session = client.session.save()
                    self.deadline = time.time() + 300
                    self.state = {
                        "status": "password_needed",
                        "message": "QR-код принят. Введите пароль двухэтапной проверки Telegram.",
                    }
                    return
                finally:
                    if not waiter.done():
                        waiter.cancel()
                    await asyncio.gather(waiter, return_exceptions=True)
                if not await asyncio.wait_for(client.is_user_authorized(), 10):
                    raise ValueError("Authorization incomplete")
                self.save(client)
                return
            self.state = {
                "status": "expired",
                "message": "Время входа истекло. Создайте новый QR-код.",
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.state = {"status": "error", "message": error_message(exc)}
        finally:
            if client:
                await self.disconnect_client(client)

    @staticmethod
    async def disconnect_client(client):
        try:
            await asyncio.wait_for(client.disconnect(), 5)
        except (Exception, asyncio.CancelledError):
            pass

    def save(self, client):
        write_secrets(self.reader.store.root, {"TG_SESSION": client.session.save()})
        self.password_session = None
        self.state = {
            "status": "authorized",
            "message": "Вход выполнен. Telegram подключён, можно читать открытые чаты.",
        }

    async def confirm(self, password):
        async with self.lock:
            if self.status()["status"] != "password_needed" or not self.password_session:
                raise ValueError("Сначала отсканируйте действующий QR-код.")
            # Wait for the QR client to disconnect before reusing its session.
            if self.task:
                await self.task
            cfg = configuration(self.reader.store.root)
            client = self.reader.factory(cfg, self.password_session)
            try:
                await asyncio.wait_for(client.connect(), 30)
                await asyncio.wait_for(client.sign_in(password=password), 30)
                if not await asyncio.wait_for(client.is_user_authorized(), 10):
                    raise ValueError("Authorization incomplete")
                self.save(client)
                return self.status()
            except errors.PasswordHashInvalidError:
                raise ValueError(
                    "Неверный пароль двухэтапной проверки. Попробуйте ещё раз."
                ) from None
            except Exception as exc:
                raise ValueError(error_message(exc)) from None
            finally:
                await self.disconnect_client(client)

    async def cancel(self):
        async with self.lock:
            if self.task and not self.task.done():
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
            self.password_session = None
            self.state = {
                "status": "idle",
                "message": "Вход по QR отменён. Можно создать новый код.",
            }
            return self.status()
