"""Accept one JSON document, optionally in one complete Markdown code block."""

import json
import re
from copy import deepcopy

from pydantic import ValidationError


def first_model_choice(response):
    choices = response.get("choices") if isinstance(response, dict) else None
    choice = choices[0] if isinstance(choices, list) and choices else None
    if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
        raise ValueError("Модель вернула ответ в неожиданном формате")
    return choice


def decode_string_arrays(value, error):
    """Restore arrays at concrete schema error paths, leaving ordinary text alone."""
    repaired = None
    for issue in error.errors():
        path, raw = issue["loc"], issue["input"]
        if issue["type"] != "list_type" or not path or not isinstance(raw, str):
            continue
        if not raw.lstrip().startswith("["):
            continue
        try:
            items = json.loads(raw)
            target = repaired if repaired is not None else deepcopy(value)
            parent = target
            for part in path[:-1]:
                parent = parent[part]
            if parent[path[-1]] == raw and isinstance(items, list):
                parent[path[-1]] = items
                repaired = target
        except (ValueError, IndexError, KeyError, TypeError, RecursionError):
            continue
    return repaired


def validate_model_data(value, schema, *, repair_arrays=False):
    try:
        return schema.model_validate(value)
    except ValidationError as error:
        repaired = decode_string_arrays(value, error) if repair_arrays else None
        if repaired is None:
            raise
        return schema.model_validate(repaired)


def parse_model_json(response, schema, *, repair_arrays=False):
    choice = first_model_choice(response)
    if choice.get("finish_reason") in {"length", "content_filter"}:
        raise ValueError("Модель не завершила ответ")
    content = choice["message"].get("content")
    if not isinstance(content, str):
        raise ValueError("Модель не вернула текст")
    fenced = re.fullmatch(r"\s*```(?:json)?[ \t]*\r?\n(.*?)\r?\n```\s*", content, re.I | re.S)
    content = fenced[1] if fenced else content
    try:
        return schema.model_validate_json(content)
    except ValidationError as error:
        if not repair_arrays or not any(row["type"] == "list_type" for row in error.errors()):
            raise
        repaired = decode_string_arrays(json.loads(content), error)
        if repaired is None:
            raise
        return schema.model_validate_json(json.dumps(repaired))


def structured_format(schema):
    """Portable strict schema; detailed bounds still run in local Pydantic validation."""
    value = schema.model_json_schema()

    def normalize(node):
        if isinstance(node, dict):
            for key in (
                "default",
                "minLength",
                "maxLength",
                "minimum",
                "maximum",
                "minItems",
                "maxItems",
                "pattern",
                "format",
            ):
                node.pop(key, None)
            if node.get("type") == "object":
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            for child in list(node.values()):
                normalize(child)
        elif isinstance(node, list):
            for child in node:
                normalize(child)

    normalize(value)
    return {
        "type": "json_schema",
        "json_schema": {
            "name": schema.__name__,
            "strict": True,
            "schema": value,
        },
    }
