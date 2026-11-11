"""Optional server authentication. Local mode retains the existing single-user workflow."""

import hashlib
import hmac
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from radar.web.configuration import configuration, write_secrets
from radar.web.store import now

COOKIE = "megapolis_session"


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(min_length=1, max_length=256)


def hash_password(password, salt=None):
    if len(password) < 12:
        raise ValueError("Пароль должен содержать не менее 12 символов")
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return salt + ":" + digest


def verify_password(password, encoded):
    try:
        salt, expected = encoded.split(":", 1)
        digest = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
        return hmac.compare_digest(digest, expected)
    except (ValueError, AttributeError):
        return False


def users(root):
    raw = configuration(root).get("RADAR_USERS", "{}")
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError()
        for name, entry in value.items():
            if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", name) or entry["role"] not in {
                "admin",
                "analyst",
                "viewer",
            }:
                raise ValueError()
            if not isinstance(entry["password_hash"], str):
                raise ValueError()
        return value
    except (ValueError, KeyError, TypeError):
        raise ValueError("Некорректна конфигурация пользователей RADAR_USERS") from None


def set_user(root, username, password, role="admin"):
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", username) or role not in {
        "admin",
        "analyst",
        "viewer",
    }:
        raise ValueError("Некорректное имя пользователя или роль")
    from radar.web.configuration import SETTINGS_LOCK

    with SETTINGS_LOCK:
        current = users(root)
        current[username] = {"password_hash": hash_password(password), "role": role}
        write_secrets(root, {"RADAR_USERS": json.dumps(current)})


def server_config(root):
    cfg = configuration(root)
    enabled = cfg.get("RADAR_SERVER_MODE", "").lower() in {"true", "1"}
    if not enabled:
        return {"enabled": False, "hosts": ["127.0.0.1", "localhost", "testserver"], "origin": None}
    origin = cfg.get("RADAR_PUBLIC_ORIGIN", "").rstrip("/")
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
    ):
        raise ValueError(
            "Серверный режим требует RADAR_PUBLIC_ORIGIN вида https://radar.example.org"
        )
    if not any(u["role"] == "admin" for u in users(root).values()):
        raise ValueError("Сначала создайте администратора: radar user-add NAME --role admin")
    return {
        "enabled": True,
        "hosts": [parsed.hostname, "127.0.0.1", "localhost", "testserver"],
        "origin": origin,
    }


def identity_version(entry):
    return hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()


class Auth:
    def __init__(self, store):
        self.store = store
        self.config = server_config(store.root)
        # Constant-work dummy verification for unknown usernames.
        self.dummy = hash_password(secrets.token_urlsafe(24))
        with store.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS auth_sessions(
                token_hash TEXT PRIMARY KEY, username TEXT NOT NULL,
                identity_version TEXT NOT NULL, expires_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS auth_attempts(ip TEXT, username TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS access_audit(
                id INTEGER PRIMARY KEY, username TEXT, method TEXT, path TEXT,
                status INTEGER, created_at TEXT);
            """)

    def session(self, token):
        if not self.config["enabled"]:
            return {"username": "local", "role": "admin", "mode": "local"}
        if not token or len(token) > 100:
            return None
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM auth_sessions WHERE token_hash=? AND expires_at>?",
                (hashlib.sha256(token.encode()).hexdigest(), now()),
            ).fetchone()
        if not row:
            return None
        entry = users(self.store.root).get(row["username"])
        if not entry or identity_version(entry) != row["identity_version"]:
            return None
        return {"username": row["username"], "role": entry["role"], "mode": "server"}

    def login(self, data, ip):
        if not self.config["enabled"]:
            raise ValueError("Локальный режим не требует входа")
        cutoff = (datetime.now(UTC) - timedelta(minutes=15)).isoformat()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM auth_attempts WHERE created_at<?", (cutoff,))
            count = db.execute(
                "SELECT count(*) FROM auth_attempts WHERE ip=? OR username=?", (ip, data.username)
            ).fetchone()[0]
            if count >= 10:
                raise ValueError("Слишком много попыток. Повторите через 15 минут")
            db.execute("INSERT INTO auth_attempts VALUES(?,?,?)", (ip, data.username, now()))
        entry = users(self.store.root).get(data.username)
        valid = verify_password(data.password, entry["password_hash"] if entry else self.dummy)
        if not entry or not valid:
            raise ValueError("Неверное имя пользователя или пароль")
        token = secrets.token_urlsafe(32)
        with self.store.connect() as db:
            db.execute("DELETE FROM auth_sessions WHERE expires_at<=?", (now(),))
            db.execute(
                "INSERT INTO auth_sessions VALUES(?,?,?,?)",
                (
                    hashlib.sha256(token.encode()).hexdigest(),
                    data.username,
                    identity_version(entry),
                    (datetime.now(UTC) + timedelta(hours=8)).isoformat(),
                ),
            )
            db.execute("DELETE FROM auth_attempts WHERE username=? AND ip=?", (data.username, ip))
        return token

    def logout(self, token):
        if token:
            with self.store.connect() as db:
                db.execute(
                    "DELETE FROM auth_sessions WHERE token_hash=?",
                    (hashlib.sha256(token.encode()).hexdigest(),),
                )

    def audit(self, identity, request, status):
        if not self.config["enabled"] or request.method in {"GET", "HEAD", "OPTIONS"}:
            return
        with self.store.connect() as db:
            db.execute(
                (
                    "INSERT INTO access_audit(username,method,path,status,created_at) "
                    "VALUES(?,?,?,?,?)"
                ),
                (
                    (identity or {}).get("username", "anonymous"),
                    request.method,
                    request.url.path[:500],
                    status,
                    now(),
                ),
            )
