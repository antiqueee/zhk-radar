import json

import pytest
from pydantic import BaseModel, ValidationError

from radar.reports.author_output import parse_author_json, parse_author_tool
from radar.reports.schema import TextEdits
from radar.web.model_output import parse_model_json


def response(content, finish_reason="stop"):
    return {"choices": [{"finish_reason": finish_reason, "message": {"content": content}}]}


@pytest.mark.parametrize(
    "wrapping",
    [
        "{}",
        "Возвращаю проверенный результат:\n{}\nГотово.",
        "Возвращаю результат:\n```json\n{}\n```\nГотово.",
        "Проверка завершена.\n```JSON\n{}\n```",
    ],
)
def test_v5_author_reads_one_complete_object_in_prose(wrapping):
    value = {"edits": [{"path": "text", "text": "Текст с {фигурными} и [квадратными] скобками"}]}
    result = parse_author_json(response(wrapping.format(json.dumps(value))), TextEdits)
    assert result.model_dump() == value


@pytest.mark.parametrize(
    "content",
    [
        "{} {}",
        "```json\n{}\n```\n```json\n{}\n```",
        "```json\n{}\n```\nЕщё объект: {",
        "```json\n{}\n```\n```json\n{",
        'Сначала незаконченный { затем {"edits": []}',
        '{"edits": []}\nНезаконченный {',
        '```json\n{"edits": []}\n',
        '[{"edits": []}]',
        '```json\n[{"edits": []}]\n```',
        "```json\n{} {}\n```",
    ],
)
def test_v5_author_does_not_choose_between_objects_or_fix_incomplete_json(content):
    with pytest.raises(ValueError, match="ровно один"):
        parse_author_json(response(content), TextEdits)


def test_v5_author_rejects_length_even_when_visible_json_looks_complete():
    value = {"edits": [{"path": "text", "text": "Текст"}]}
    with pytest.raises(ValueError, match="не завершила"):
        parse_author_json(response(json.dumps(value), "length"), TextEdits)


def test_v5_author_still_runs_the_complete_pydantic_contract():
    with pytest.raises(ValidationError):
        parse_author_json(response('Результат: {"edits": [{"path": "text"}]}'), TextEdits)


def test_other_model_parser_keeps_its_existing_strict_boundary():
    value = {"edits": [{"path": "text", "text": "Текст"}]}
    content = "Проверенный JSON:\n```json\n" + json.dumps(value) + "\n```"
    with pytest.raises(ValueError):
        parse_model_json(response(content), TextEdits)


def tool_response(arguments, name="return_report_data", count=1, finish_reason="tool_calls"):
    return {
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {
                    "tool_calls": [
                        {"type": "function", "function": {"name": name, "arguments": arguments}}
                        for _ in range(count)
                    ]
                },
            }
        ]
    }


def test_forced_author_data_tool_preserves_quotes_without_executing_anything():
    value = {"edits": [{"path": "official_name", "text": 'ЖК «Светлый мир „Станция Л"»'}]}
    result = parse_author_tool(tool_response(json.dumps(value)), TextEdits)
    assert result.model_dump() == value


@pytest.mark.parametrize("mode", ["tool", "author_text", "schema_text"])
def test_v8_output_restores_nested_arrays_and_preserves_prose(mode):
    class EditBatches(BaseModel):
        batches: list[TextEdits]

    edits = [{"path": "official_name", "text": '["Оставить текст как есть"]'}]
    body = json.dumps({"batches": [{"edits": json.dumps(edits)}]})
    if mode == "tool":
        result = parse_author_tool(tool_response(body), EditBatches, repair_arrays=True)
    elif mode == "author_text":
        result = parse_author_json(response(body), EditBatches, repair_arrays=True)
    else:
        result = parse_model_json(response(body), EditBatches, repair_arrays=True)
    assert result.model_dump() == {"batches": [{"edits": edits}]}


@pytest.mark.parametrize(
    "arguments,name,count,finish",
    [
        ("{}", "return_report_data", 0, "tool_calls"),
        ("{}", "return_report_data", 2, "tool_calls"),
        ("{}", "another_function", 1, "tool_calls"),
        ("{} {}", "return_report_data", 1, "tool_calls"),
        ('{"edits": [', "return_report_data", 1, "tool_calls"),
        ("[]", "return_report_data", 1, "tool_calls"),
        ("{}", "return_report_data", 1, "length"),
    ],
)
def test_forced_author_data_tool_requires_one_exact_complete_call(arguments, name, count, finish):
    with pytest.raises(ValueError):
        parse_author_tool(tool_response(arguments, name, count, finish), TextEdits)
