"""Opt-in TLS email and encrypted S3 backups; never auto-retry uncertain messages."""

import base64
import hashlib
import io
import os
import re
import smtplib
import ssl
import tarfile
import threading
from email.message import EmailMessage
from pathlib import Path

import boto3
from botocore.config import Config
from cryptography.fernet import Fernet
from pydantic import BaseModel, ConfigDict

from radar.web.configuration import configuration
from radar.web.maintenance import backup
from radar.web.store import now

LOCK = threading.Lock()


class ExternalSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email_digest: bool = False
    cloud_backup: bool = False


def initialize(store):
    with store.connect() as db:
        db.executescript("""CREATE TABLE IF NOT EXISTS external_settings(
            id INTEGER PRIMARY KEY,payload TEXT);
            CREATE TABLE IF NOT EXISTS email_outbox(
                id TEXT PRIMARY KEY,status TEXT,error TEXT,created_at TEXT);
            CREATE TABLE IF NOT EXISTS cloud_backups(
                id TEXT PRIMARY KEY,bucket TEXT,object_key TEXT,
                status TEXT,error TEXT,created_at TEXT);""")
        db.execute(
            "INSERT OR IGNORE INTO external_settings VALUES(1,?)",
            (ExternalSettings().model_dump_json(),),
        )


def settings(store):
    initialize(store)
    with store.connect() as db:
        return ExternalSettings.model_validate_json(
            db.execute("SELECT payload FROM external_settings WHERE id=1").fetchone()[0]
        )


def status(store):
    value = settings(store)
    with store.connect() as db:
        return {
            "settings": value.model_dump(),
            "email": [
                dict(r)
                for r in db.execute("SELECT * FROM email_outbox ORDER BY created_at DESC LIMIT 10")
            ],
            "backups": [
                dict(r)
                for r in db.execute("SELECT * FROM cloud_backups ORDER BY created_at DESC LIMIT 10")
            ],
        }


def save(store, value):
    initialize(store)
    with store.connect() as db:
        db.execute("UPDATE external_settings SET payload=? WHERE id=1", (value.model_dump_json(),))
    return value.model_dump()


def send_mail(cfg, text, transport=smtplib.SMTP_SSL):
    required = ["SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM", "SMTP_TO"]
    if not all(cfg.get(k) for k in required):
        raise ValueError("Заполните SMTP и адреса рабочей рассылки")
    addresses = [cfg["SMTP_FROM"], *[s.strip() for s in cfg["SMTP_TO"].split(",")]]
    if len(addresses) > 11 or any(
        not re.fullmatch(r"[^\s@<>\r\n]+@[^\s@<>\r\n]+\.[^\s@<>\r\n]+", a) for a in addresses
    ):
        raise ValueError("Некорректные адреса рабочей рассылки")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", cfg["SMTP_HOST"]):
        raise ValueError("Некорректный SMTP-сервер")
    message = EmailMessage()
    message["Subject"] = "Мегаполис Интеледженс — недельный мониторинг"
    message["From"] = addresses[0]
    message["To"] = ", ".join(addresses[1:])
    message.set_content(text)
    try:
        with transport(
            cfg["SMTP_HOST"], 465, context=ssl.create_default_context(), timeout=25
        ) as smtp:
            smtp.login(cfg["SMTP_USERNAME"], cfg["SMTP_PASSWORD"])
            refused = smtp.send_message(message)
            if refused:
                raise ValueError("Не все адресаты приняли письмо")
    except Exception:
        raise ValueError(
            "Почта не подтвердила полную доставку. Проверьте ящик; автоматического повтора нет."
        ) from None


def deliver_email(store, ident, text, sender=send_mail):
    initialize(store)
    cfg = configuration(store.root)
    if not settings(store).email_digest:
        return {"status": "disabled"}
    if not all(
        cfg.get(k) for k in ["SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM", "SMTP_TO"]
    ):
        with store.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO email_outbox VALUES(?, 'blocked', ?, ?)",
                (ident, "Заполните SMTP и адреса рассылки. Письмо не отправлено.", now()),
            )
        return {"status": "blocked"}
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT status FROM email_outbox WHERE id=?", (ident,)).fetchone()
        if old:
            return {"status": old[0]}
        db.execute("INSERT INTO email_outbox VALUES(?,'sending',NULL,?)", (ident, now()))
    try:
        sender(cfg, text)
        state, error = "sent", None
    except Exception:
        state, error = (
            "uncertain",
            "Полная доставка не подтверждена. Повтор только после проверки ящика.",
        )
    with store.connect() as db:
        db.execute("UPDATE email_outbox SET status=?,error=? WHERE id=?", (state, error, ident))
    return {"status": state, "error": error}


def s3_client(cfg):
    return boto3.client(
        "s3",
        region_name=cfg["BACKUP_REGION"],
        aws_access_key_id=cfg["BACKUP_ACCESS_KEY"],
        aws_secret_access_key=cfg["BACKUP_SECRET_KEY"],
        config=Config(
            connect_timeout=10,
            read_timeout=60,
            retries={"total_max_attempts": 1},
            ignore_configured_endpoint_urls=True,
        ),
    )


def encrypted_archive(store, backup_file, key):
    if backup_file.stat().st_size > 100 * 1024 * 1024:
        raise ValueError("Копия больше 100 МБ: настройте потоковое резервирование сервера")
    cipher = Fernet(key.encode())
    stream = io.BytesIO()
    # Only allowlisted contents; encryption key and service credentials never leave the server.
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        archive.add(backup_file, arcname="workspace.sqlite3")
        for config in sorted((store.root / "config").glob("*.yaml")):
            if config.is_file() and not config.is_symlink():
                archive.add(config, arcname="config/" + config.name)
    return cipher.encrypt(stream.getvalue())


def upload_backup(store, factory=s3_client):
    if not LOCK.acquire(blocking=False):
        raise ValueError("Внешняя копия уже создаётся")
    try:
        initialize(store)
        cfg = configuration(store.root)
        required = [
            "BACKUP_REGION",
            "BACKUP_BUCKET",
            "BACKUP_ACCESS_KEY",
            "BACKUP_SECRET_KEY",
            "BACKUP_ENCRYPTION_KEY",
        ]
        if not all(cfg.get(k) for k in required):
            raise ValueError("Заполните доступ к S3 и ключ шифрования")
        if not re.fullmatch(
            r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", cfg["BACKUP_BUCKET"]
        ) or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d", cfg["BACKUP_REGION"]):
            raise ValueError("Проверьте регион AWS и имя bucket")
        try:
            Fernet(cfg["BACKUP_ENCRYPTION_KEY"].encode())
        except (ValueError, TypeError):
            raise ValueError("Нужен ключ Fernet: 32 байта в urlsafe base64") from None
        result = backup(store, automatic=True)
        path = store.root / "data/backups" / result["file"]
        ident = hashlib.sha256(
            (
                result["file"]
                + cfg["BACKUP_BUCKET"]
                + cfg["BACKUP_REGION"]
                + hashlib.sha256(cfg["BACKUP_ENCRYPTION_KEY"].encode()).hexdigest()
            ).encode()
        ).hexdigest()
        key = "megapolis/" + ident + ".tar.gz.fernet"
        with store.connect() as db:
            old = db.execute("SELECT status FROM cloud_backups WHERE id=?", (ident,)).fetchone()
            if old and old[0] == "completed":
                return {"status": "completed", "object_key": key, "cached": True}
        data = encrypted_archive(store, path, cfg["BACKUP_ENCRYPTION_KEY"])
        try:
            client = factory(cfg)
            try:
                client.put_object(
                    Bucket=cfg["BACKUP_BUCKET"],
                    Key=key,
                    Body=data,
                    ContentType="application/octet-stream",
                    ServerSideEncryption="AES256",
                    ChecksumSHA256=base64.b64encode(hashlib.sha256(data).digest()).decode(),
                )
            finally:
                client.close()
            state, error = "completed", None
        except Exception:
            state, error = "failed", "Хранилище не подтвердило запись зашифрованной копии"
        with store.connect() as db:
            db.execute(
                "INSERT INTO cloud_backups VALUES(?,?,?,?,?,?) ON CONFLICT(id) "
                "DO UPDATE SET status=excluded.status,error=excluded.error,"
                "created_at=excluded.created_at",
                (ident, cfg["BACKUP_BUCKET"], key, state, error, now()),
            )
        if error:
            raise ValueError(error)
        return {"status": state, "object_key": key, "cached": False}
    finally:
        LOCK.release()


def decrypt_archive(source: Path, destination: Path, key: str):
    # Never extract filenames from the archive automatically. Operator restores after inspection.
    data = Fernet(key.encode()).decrypt(source.read_bytes())
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(data)
