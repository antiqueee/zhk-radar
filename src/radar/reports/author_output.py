"""Read one authored JSON object and validate its contents."""

import json
import re

from radar.web.model_output import first_model_choice, validate_model_data

AUTHOR_TOOL = "return_report_data"


def parse_author_tool(response, contract, *, repair_arrays=False):
    """Read a forced data-return call. No function is ever executed."""
    choice = first_model_choice(response)
    if choice.get("finish_reason") in {"length", "content_filter"}:
        raise ValueError("Модель не завершила ответ")
    calls = choice.get("message", {}).get("tool_calls") or []
    if (not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict)
            or calls[0].get("type") != "function"):
        raise ValueError("Модель должна вернуть ровно один вызов return_report_data")
    function = calls[0].get("function") or {}
    if (not isinstance(function, dict) or function.get("name") != AUTHOR_TOOL
            or not isinstance(function.get("arguments"), str)):
        raise ValueError("Модель вернула другой вызов или неверный формат аргументов")
    try:
        value = json.loads(function["arguments"])
    except ValueError:
        raise ValueError("Аргументы return_report_data должны быть полным JSON-объектом") from None
    if not isinstance(value, dict):
        raise ValueError("Аргументы return_report_data должны быть JSON-объектом")
    return validate_model_data(value, contract, repair_arrays=repair_arrays)


def parse_author_json(response, contract, *, repair_arrays=False):
    choice = first_model_choice(response)
    if choice.get("finish_reason") in {"length", "content_filter"}:
        raise ValueError("Модель не завершила ответ")
    content = choice.get("message", {}).get("content")
    if not isinstance(content, str):
        raise ValueError("Модель не вернула текст")
    message = "Модель должна вернуть ровно один завершённый JSON-объект"
    if "```" in content:
        if content.count("```") != 2:
            raise ValueError(message)
        fence = re.search(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n```", content, re.I | re.S)
        if fence is None:
            raise ValueError(message)
        outside = content[: fence.start()] + content[fence.end() :]
        if any(char in outside for char in "{}[]"):
            raise ValueError(message)
        try:
            value = json.loads(fence[1])
        except ValueError:
            raise ValueError(message) from None
    else:
        start = next((i for i, char in enumerate(content) if char in "{}[]"), None)
        if start is None or content[start] != "{":
            raise ValueError(message)
        try:
            value, end = json.JSONDecoder().raw_decode(content, start)
        except ValueError:
            raise ValueError(message) from None
        outside = content[:start] + content[end:]
        if any(char in outside for char in "{}[]"):
            raise ValueError(message)
    if not isinstance(value, dict):
        raise ValueError(message)
    return validate_model_data(value, contract, repair_arrays=repair_arrays)
