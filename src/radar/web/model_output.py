"""Accept one JSON document, optionally in one complete Markdown code block."""

import re


def parse_model_json(response, schema):
    choice = response["choices"][0]
    if choice.get("finish_reason") in {"length", "content_filter"}:
        raise ValueError("Модель не завершила ответ")
    content = choice["message"]["content"]
    if not isinstance(content, str):
        raise ValueError("Модель не вернула текст")
    fenced = re.fullmatch(r"\s*```(?:json)?[ \t]*\r?\n(.*?)\r?\n```\s*", content, re.I | re.S)
    return schema.model_validate_json(fenced[1] if fenced else content)


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
