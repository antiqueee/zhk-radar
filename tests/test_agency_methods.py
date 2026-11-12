import io
import json

import pytest
from pypdf import PdfReader

from radar.briefs import methods
from radar.web import author, conversation
from radar.web.pdf import render_pdf


def selected(text, **kwargs):
    return methods.select([{"id": "live-source", "text": text}], **kwargs)


def test_all_sources_reviewed_and_methods_have_traceable_origins():
    library = methods.Library.model_validate_json(methods.LIBRARY_PATH.read_bytes())
    assert len(library.sources) == 31
    assert {s.id for s in library.sources} == {f"AG{i:02}" for i in range(1, 32)}
    assert len(library.methods) == 15
    assert {r.source_id for m in library.methods for r in m.references} == {
        s.id for s in library.sources
    }
    assert all(len(s.review) > 100 for s in library.sources)


@pytest.mark.parametrize(
    ("text", "expected", "absent"),
    [
        ("Повторный перенос срока, стройка стоит, нет рабочих.", "M01", "M09"),
        ("Выдача ключей началась, приёмка квартир и заселение.", "M04", "M07"),
        ("Протечки, дефекты стеклопакетов, гарантийные заявки.", "M05", "M01"),
        ("Соседи против стройплощадки, шум и перенос ограждения.", "M07", "M04"),
        ("ОСС, тарифы, охрана, уборка управляющей компании.", "M09", "M01"),
        ("Перевод апартаментов в жилой фонд и договор с отельером.", "M10", "M05"),
    ],
)
def test_methods_follow_object_problem_not_old_client(text, expected, absent):
    ids = {m["id"] for m in selected(text)["methods"]}
    assert expected in ids and absent not in ids


def test_unrelated_question_is_scenario_and_missing_facts_stay_empty():
    assert not methods.select([])["methods"]
    context = methods.select([], question="Как проводить переговоры с инициативной группой?")
    assert context["methods"]
    assert all(m["selection_basis"] == "question_only" for m in context["methods"])


def test_context_is_bounded_and_does_not_export_archive_clients_or_prices():
    library = methods.Library.model_validate_json(methods.LIBRARY_PATH.read_bytes())
    context = selected(" ".join(t for m in library.methods for t in m.triggers))
    encoded = json.dumps(context, ensure_ascii=False)
    assert len(context["methods"]) <= methods.MAX_METHODS
    assert len(encoded) <= methods.MAX_CONTEXT_CHARS
    assert "filename" not in encoded and "sha256" not in encoded
    for word in ["Самолет", "Самолёт", "СберСити", "НЕОМЕТРИЯ", "Наметкин", "678405", "₽"]:
        assert word not in encoded
    assert all(m["references"] for m in context["methods"])


def test_changed_library_invalidates_context_even_if_selected_method_unchanged(
    tmp_path, monkeypatch
):
    before = selected("перенос срока")
    data = json.loads(methods.LIBRARY_PATH.read_text())
    data["methods"][-1]["resource_choice"] += " Уточнение."
    path = tmp_path / "methods.json"
    path.write_text(json.dumps(data))
    monkeypatch.setattr(methods, "LIBRARY_PATH", path)
    after = selected("перенос срока")
    assert before["fingerprint"] != after["fingerprint"]
    assert before["methods"] == after["methods"]


def test_offline_report_uses_only_compatible_methods_and_stays_two_pages():
    context = selected("Повторный перенос срока, нет рабочих, жалобы в чате.")
    report = {
        "products": [dict(code="O2", name="Доверие при переносе", tasks=[], matching_topics=[])]
    }
    methods.apply_to_report(report, context)
    product = report["products"][0]
    assert "M01" in product["method_candidates"]
    assert product["method_ids"] == ["M01"]
    assert "инженер" in " ".join(product["tasks"])
    payload = dict(
        name="Тестовый ЖК, корпус Б",
        dataset="demo",
        summary="Учебный перенос срока.",
        report=report,
    )
    pdf = PdfReader(io.BytesIO(render_pdf(payload)))
    assert len(pdf.pages) == 2
    assert "МЕГАПОЛИС" in pdf.pages[1].extract_text()
    assert "31" not in pdf.pages[0].extract_text()


def test_archive_method_cannot_be_factual_citation_or_unselected_method():
    product = dict(code="O2", matching_topics=["перенос_сроков"], method_candidates=["M01"])
    payload = dict(report={"products": [product]})
    source = dict(id="actual", topics=["перенос_сроков"])
    draft = author.Authored(
        products=[
            author.ProductText(
                code="O2",
                rationale="По источнику есть вопросы о сроках передачи квартир.",
                tasks=["Согласовать понятный ответ инженера по этапам работ."] * 3,
                result="Покупатели понимают этапы работ и следующий контакт.",
                measurement="Сравнить повторные обращения по срокам.",
                source_ids=["AG01"],
                method_ids=["M01"],
            )
        ]
    )
    with pytest.raises(ValueError, match="источник"):
        author.validate(draft, payload, [source], [])
    draft.products[0].source_ids = ["actual"]
    draft.products[0].method_ids = ["M09"]
    with pytest.raises(ValueError, match="метод"):
        author.validate(draft, payload, [source], [])


def test_writer_and_dialog_receive_methods_as_separate_context(monkeypatch):

    calls = []

    def capture(client, url, headers, body, label):
        calls.append(body)
        raise ValueError("Captured")

    monkeypatch.setattr(author, "call_json", capture)
    monkeypatch.setattr(conversation, "call_json", capture)
    context = selected("Перенос срока")
    payload = dict(
        name="Тестовый ЖК", situation="сроки", report={"products": []}, agency_knowledge=context
    )
    with pytest.raises(ValueError, match="Captured"):
        author.write_model(
            None, {"OPENROUTER_API_KEY": "fixture"}, {"model": "test"}, payload, [], [], None
        )
    with pytest.raises(ValueError, match="Captured"):
        conversation.write_reply(
            None,
            {"OPENROUTER_API_KEY": "fixture"},
            {"model": "test"},
            {"agency_knowledge": context, "sources": []},
        )
    for call in calls:
        value = json.loads(call["messages"][1]["content"])
        assert value["agency_knowledge"]["methods"]
        assert value["sources"] == []


def test_verbatim_recipe_is_rejected_as_unadapted():
    context = selected("Перенос срока")
    method = next(m for m in context["methods"] if m["id"] == "M01")
    payload = dict(
        agency_knowledge=context,
        report={"products": [dict(code="O2", matching_topics=[], method_candidates=["M01"])]},
    )
    draft = author.Authored(
        products=[
            author.ProductText(
                code="O2",
                rationale="В источнике описан перенос срока конкретного корпуса.",
                tasks=method["steps"],
                result=method["result"],
                measurement=method["measurement"],
                source_ids=["current"],
                method_ids=["M01"],
            )
        ]
    )
    with pytest.raises(ValueError, match="без адаптации"):
        author.validate(draft, payload, [dict(id="current", topics=[])], [])
