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
}


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: dict[str, str] = Field(default_factory=dict, max_length=12)
    model: str = Field(default="", max_length=150)

    @field_validator("values")
    @classmethod
    def check_values(cls, values):
        if not values.keys() <= KEYS:
            raise ValueError("Неизвестное подключение")
        if any(not v or len(v) > 2000 or any(c in v for c in "\r\n\x00") for v in values.values()):
            raise ValueError("Некорректное значение подключения")
        return values

    @field_validator("model")
    @classmethod
    def check_model(cls, value):
        if value and not re.fullmatch(r"[a-zA-Z0-9._:/-]+", value):
            raise ValueError("Некорректный идентификатор модели")
        return value


def configuration(root: Path):
    return {**os.environ, **dotenv_values(root / ".env", interpolate=False)}


def model_config(root: Path):
    path = root / "config/models.yaml"
    return yaml.safe_load(path.read_text()) if path.exists() else {"extract": {"model": ""}}


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
        cfg = model_config(root)
        cfg.setdefault("extract", {})["model"] = update.model
        atomic_write(root / "config/models.yaml", yaml.safe_dump(cfg, allow_unicode=True), 0o644)
