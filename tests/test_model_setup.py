import json
import shutil

import httpx
import yaml

from radar.web.configuration import (
    SettingsUpdate,
    model_config,
    model_options,
    raw_model_config,
    save_settings,
)
from radar.web.model_catalog import fetch_catalog


def test_defaults_keys_only_and_explicit_override(tmp_path):
    shutil.copytree("config", tmp_path / "config")
    before = raw_model_config(tmp_path)
    assert all(before[r]["default_model"] for r in ("extract", "author", "review"))
    save_settings(tmp_path, SettingsUpdate(values={"OPENROUTER_API_KEY": "test-key"}))
    cfg = model_config(tmp_path)
    assert cfg["extract"]["model"] == before["extract"]["default_model"]
    assert cfg["author"]["model"] != cfg["review"]["model"]
    assert raw_model_config(tmp_path)["extract"]["model"] == ""
    save_settings(tmp_path, SettingsUpdate(model="custom/extractor"))
    save_settings(tmp_path, SettingsUpdate(values={"YANDEX_FOLDER_ID": "test-folder"}))
    assert model_config(tmp_path)["extract"]["model"] == "custom/extractor"
    save_settings(tmp_path, SettingsUpdate(model=""))
    assert model_config(tmp_path)["extract"]["model"] == before["extract"]["default_model"]
    options = model_options(cfg["review"])
    assert "temperature" not in options
    assert options["reasoning"] == {"effort": "low"}
    assert "default_model" not in options
    assert "test-key" not in (tmp_path / "config/models.yaml").read_text()
    assert (
        yaml.safe_load((tmp_path / "config/models.yaml").read_text())["author"] == before["author"]
    )


def test_catalog_filters_and_normalizes():
    def handle(request):
        assert str(request.url) == "https://openrouter.ai/api/v1/models"
        assert "authorization" not in request.headers
        base = {
            "architecture": {"output_modalities": ["text"]},
            "supported_parameters": ["response_format"],
            "pricing": {"prompt": "0.000001", "completion": "0.000005"},
        }
        return httpx.Response(
            200,
            json={
                "data": [
                    {**base, "id": "test/valid", "name": "Valid"},
                    {**base, "id": "test/valid:batch"},
                    {**base, "id": "test/nojson", "supported_parameters": []},
                    {**base, "id": "test/image", "architecture": {"output_modalities": ["image"]}},
                    {
                        **base,
                        "id": "test/badprice",
                        "pricing": {"prompt": "NaN", "completion": "-1"},
                    },
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        rows = fetch_catalog(client)
    assert {r["id"] for r in rows} == {"test/valid", "test/badprice"}
    valid = next(r for r in rows if r["id"] == "test/valid")
    assert valid["prompt"] == 1 and valid["completion"] == 5
    assert "NaN" not in json.dumps(rows)


def test_model_json_fence_keeps_schema_and_rejects_truncation():
    import pytest

    from radar.web.discovery import Findings
    from radar.web.model_output import parse_model_json

    def response(content, reason="stop"):
        return {"choices": [{"message": {"content": content}, "finish_reason": reason}]}

    assert parse_model_json(response('```json\n{"objects": []}\n```'), Findings).objects == []
    for content in [
        'comment\n{"objects": []}',
        '```json\n{"unknown": []}\n```',
        '```json\n{"objects": []}\n```\nmore',
    ]:
        with pytest.raises(ValueError):
            parse_model_json(response(content), Findings)
    with pytest.raises(ValueError):
        parse_model_json(response('{"objects": []}', "length"), Findings)
