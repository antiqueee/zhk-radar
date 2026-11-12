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
