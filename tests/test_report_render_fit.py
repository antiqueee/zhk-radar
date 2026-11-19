"""The compact alternative reclaims whitespace; diagnostics retain actual field paths."""

from copy import deepcopy

from radar.reports.render import OVERFLOW_JS, V8_FIT_CSS, _report_text_fields


def test_fit_overrides_are_strictly_scoped_and_preserve_typography_and_page_geometry():
    for rule in V8_FIT_CSS.strip().splitlines():
        assert rule.startswith(".page.is-fit-compact[data-page] ")
    for forbidden in [
        "font", "line-height", "transform", "scale", "zoom", "height:", "width:",
        "overflow", "display:none", "line-clamp",
    ]:
        assert forbidden not in V8_FIT_CSS
    assert ".body{gap:2mm;" in V8_FIT_CSS


def test_overflow_text_fields_are_exact_paths_without_changing_evidence_or_exported_copy():
    data = {
        "meta": {"date": "17.11.2026"},
        "object": {"name": "ЖК Тест"},
        "situation": {"lead": "Длинная важная оговорка. " * 40},
        "priority": {"criteria": [{"why": "Подтверждённая причина оценки"}]},
        "offer": {"modules": [{
            "name": "Переговоры", "move": "I04", "service_ids": ["S01"], "method_ids": ["M01"],
            "agency": ["Ведём переговоры по требованиям"], "pain_refs": [1],
        }]},
        "diagnosis": {"sources": [{"url": "https://source.example", "says": "Содержание"}]},
    }
    original = deepcopy(data)
    fields = {row["path"]: row["text"] for row in _report_text_fields(data)}
    assert fields["situation.lead"] == data["situation"]["lead"]
    assert fields["priority.criteria.0.why"] == "Подтверждённая причина оценки"
    assert fields["offer.modules.0.agency.0"] == "Ведём переговоры по требованиям"
    assert fields["diagnosis.sources.0.says"] == "Содержание"
    assert not any(key.startswith(("meta.", "object.")) or key.endswith(".url")
                   or ".service_ids." in key or ".method_ids." in key or key.endswith(".move")
                   for key in fields)
    assert data == original


def test_overflow_diagnostics_keep_existing_geometry_checks_and_add_targeted_field_hints():
    assert "body.scrollHeight>body.clientHeight+1" in OVERFLOW_JS
    assert "['hidden','clip']" in OVERFLOW_JS
    assert "field_paths:matching.map(f=>f.path)" in OVERFLOW_JS
    assert "text_fields:matching.slice(0,6)" in OVERFLOW_JS
    assert "prefixes.some(prefix=>f.path.startsWith(prefix))" in OVERFLOW_JS
