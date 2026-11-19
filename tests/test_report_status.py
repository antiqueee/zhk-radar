import pytest

from radar.web.report_status import download_status, require_download


def test_partial_failed_search_does_not_present_watch_as_final_conclusion():
    payload = {
        "report_qualification": {"verdict": "watch", "reasons": ["below_threshold"]},
        "report_pipeline": {"status": "watch", "passed": False},
        "report_research": {
            "completed": False,
            "errors": [{"stage": "search", "detail": "429"}],
            "documents": [{"read_status": "loaded", "text": "Сохранённая статья"}],
        },
    }
    result = download_status(payload)
    assert result["downloadable"] is False
    assert result["label"] == "Поиск завершён частично"
    assert "Материалы сохранены" in result["detail"]
    assert "Недостаточно датированных" not in result["detail"]
    with pytest.raises(ValueError, match="Поиск завершён частично"):
        require_download(payload)


def test_completed_watch_retains_its_evidence_reason():
    payload = {
        "report_qualification": {"verdict": "watch", "reasons": ["below_threshold"]},
        "report_research": {"completed": True, "errors": []},
    }
    result = download_status(payload)
    assert result["label"] == "Объект оставлен под наблюдением"
    assert result["detail"] == "Недостаточно датированных признаков текущего конфликта"


def test_qualified_reviewed_pdf_remains_downloadable_after_partial_search():
    payload = {
        "report_v2": {"meta": {"schema_version": "8"}},
        "report_qualification": {"verdict": "report_full"},
        "report_pipeline": {"status": "passed", "passed": True},
        "report_research": {"completed": False, "errors": [{"stage": "search"}]},
    }
    assert download_status(payload)["downloadable"] is True


@pytest.mark.parametrize("report", [None, {"meta": {"schema_version": "5"}}])
def test_v8_status_cannot_authorize_a_different_document_format(report):
    payload = {"report_v2": report,
               "report_pipeline": {"version": "report-kit-v8", "status": "passed", "passed": True}}
    result = download_status(payload)
    assert result["downloadable"] is False
    assert "отсутствует документ V8" in result["detail"]


def test_unavailable_page_is_not_misreported_as_failed_search():
    payload = {
        "report_pipeline": {"status": "manual", "passed": False},
        "report_research": {"completed": False, "errors": [{"stage": "entities"}]},
    }
    assert download_status(payload)["label"] == "Нужна редакторская проверка"


@pytest.mark.parametrize("issue", [
    {"issue_kind": "model_output"},
    {"issues": ["Модель повторила те же замечания: 1 validation error for RawAnchoredResearch\n"
                "stage_signs\nInput should be a valid list"]},
])
def test_saved_model_output_failure_offers_retry(issue):
    payload = {
        "report_pipeline": {"version": "report-kit-v8", "status": "manual", "passed": False,
                            **issue},
    }
    result = download_status(payload)
    assert result["downloadable"] is False
    assert result["label"] == "Не удалось разобрать ответ модели"
    assert result["retryable"] is True
    with pytest.raises(ValueError, match=result["label"]):
        require_download(payload)
