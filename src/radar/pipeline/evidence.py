import hashlib
from datetime import date, timedelta
from typing import Literal


def validate_quote(text: str, quote: str) -> None:
    if not quote.strip() or quote not in text:
        raise ValueError("Цитата не найдена дословно в документе")


def dedup_key(
    kind: str, level: Literal["object", "complex", "developer"], entity_id: str, occurred_at: date
) -> str:
    if not kind or not entity_id:
        raise ValueError("Нужны тип сигнала и идентификатор сущности")
    monday = occurred_at - timedelta(days=occurred_at.weekday())
    value = f"{kind}\0{level}\0{entity_id}\0{monday.isoformat()}"
    return hashlib.sha256(value.encode()).hexdigest()
