"""Public model catalog for optional selection; never generates completions."""

import math
import re
import threading
import time

import httpx

LOCK = threading.Lock()
CACHE = {"expires": 0, "models": []}


def fetch_catalog(client):
    response = client.get("https://openrouter.ai/api/v1/models")
    response.raise_for_status()
    models = []
    for item in response.json()["data"]:
        ident = item.get("id", "")
        if (
            not re.fullmatch(r"[a-zA-Z0-9._:/-]{1,150}", ident)
            or ident.endswith(":batch")
            or "text" not in item.get("architecture", {}).get("output_modalities", [])
            or "response_format" not in item.get("supported_parameters", [])
        ):
            continue
        prices = {}
        for key in ("prompt", "completion"):
            try:
                value = float(item.get("pricing", {}).get(key, "nan")) * 1_000_000
                prices[key] = round(value, 4) if math.isfinite(value) and value >= 0 else None
            except (ValueError, TypeError):
                prices[key] = None
        models.append({"id": ident, "name": str(item.get("name", ident))[:180], **prices})
    return sorted(models, key=lambda m: m["name"].lower())


def catalog():
    with LOCK:
        if CACHE["expires"] > time.time():
            return {"models": CACHE["models"], "cached": True}
        try:
            with httpx.Client(timeout=15) as client:
                rows = fetch_catalog(client)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise ValueError(
                "Не удалось загрузить каталог OpenRouter. Текущие модели сохранены."
            ) from None
        CACHE.update(expires=time.time() + 3600, models=rows)
        return {"models": rows, "cached": False}
