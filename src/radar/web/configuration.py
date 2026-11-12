"""Local settings: values never returned by the HTTP API."""

import os
import re
import tempfile
import threading
from pathlib import Path

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, field_validator

SETTINGS_LOCK = threading.RLock()
KEYS = {
    "YANDEX_SEARCH_API_KEY",
    "YANDEX_FOLDER_ID",
    "OPENROUTER_API_KEY",
    "TG_API_ID",
    "TG_API_HASH",
    "TG_BOT_TOKEN",
    "TG_GROUP_ID",
    "GOOGLE_SA_JSON",
    "SHEET_ID",
    "PERPLEXITY_API_KEY",
    "BRAVE_SEARCH_API_KEY",
    "VK_SERVICE_TOKEN",
    "HH_USER_AGENT",
    "SMTP_HOST",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "SMTP_FROM",
    "SMTP_TO",
    "BACKUP_REGION",
    "BACKUP_BUCKET",
    "BACKUP_ACCESS_KEY",
    "BACKUP_SECRET_KEY",
    "BACKUP_ENCRYPTION_KEY",
}


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: dict[str, str] = Field(default_factory=dict, max_length=48)
    model: str | None = Field(default=None, max_length=150)
    review_model: str | None = Field(default=None, max_length=150)
    author_model: str | None = Field(default=None, max_length=150)

    @field_validator("values")
    @classmethod
    def check_values(cls, values):
        if not values.keys() <= KEYS:
            raise ValueError("Неизвестное подключение")
        if any(not v or len(v) > 2000 or any(c in v for c in "\r\n\x00") for v in values.values()):
            raise ValueError("Некорректное значение подключения")
        return values

    @field_validator("model", "review_model", "author_model")
    @classmethod
    def check_model(cls, value):
        if value and not re.fullmatch(r"[a-zA-Z0-9._:/-]+", value):
            raise ValueError("Некорректный идентификатор модели")
        return value


def configuration(root: Path):
    return {**os.environ, **dotenv_values(root / ".env", interpolate=False)}


def raw_model_config(root: Path):
    path = root / "config/models.yaml"
    return yaml.safe_load(path.read_text()) if path.exists() else {"extract": {"model": ""}}


def model_config(root: Path):
    cfg = raw_model_config(root)
    for value in cfg.values():
        if isinstance(value, dict) and not value.get("model"):
            value["model"] = value.get("default_model", "")
    return cfg


def model_options(model, max_tokens=5000):
    result = {"model": model["model"], "max_tokens": model.get("max_tokens", max_tokens)}
    if model.get("temperature", 0) is not None:
        result["temperature"] = model.get("temperature", 0)
    if model.get("reasoning"):
        result["reasoning"] = model["reasoning"]
    return result


def atomic_write(path: Path, text: str, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".settings-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_secrets(root: Path, updates: dict):
    with SETTINGS_LOCK:
        path = root / ".env"
        values = {**dotenv_values(path, interpolate=False), **updates}
        lines = []
        for key, value in values.items():
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key) or value is None:
                continue
            escaped = value.replace("\\", "\\\\").replace("'", "\\'")
            lines.append(f"{key}='{escaped}'")
        atomic_write(path, "\n".join(lines) + "\n")


def save_settings(root: Path, update: SettingsUpdate):
    with SETTINGS_LOCK:
        write_secrets(root, update.values)
        cfg = raw_model_config(root)
        if update.model is not None:
            cfg.setdefault("extract", {})["model"] = update.model
        if update.review_model is not None:
            cfg.setdefault("review", {})["model"] = update.review_model
        if update.author_model is not None:
            cfg.setdefault("author", {})["model"] = update.author_model
        atomic_write(root / "config/models.yaml", yaml.safe_dump(cfg, allow_unicode=True), 0o644)
