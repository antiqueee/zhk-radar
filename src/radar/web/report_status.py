"""One status policy for saved reports, task results and client exports."""

import re

LABELS = {
    "hold": "Нужна проверка объекта",
    "watch": "Объект оставлен под наблюдением",
    "manual": "Нужна редакторская проверка",
}
REASONS = {
    "developer_unknown": "Не установлен застройщик",
    "object_delivered": "Объект введён или ключи выданы — нужно уточнить текущую задачу",
    "only_post_delivery_topics": "Найдены только вопросы вне строительства и обязательств",
    "fund_or_city": "Достройкой занимается город или фонд — нужно выбрать адресата",
    "no_solvent_addressee": "Не подтверждён действующий адресат достройки",
    "current_status_or_addressee_unverified": "Не подтверждены текущий статус или адресат",
    "below_threshold": "Недостаточно датированных признаков текущего конфликта",
}


def report_format_version(payload):
    meta = (payload.get("report_v2") or {}).get("meta") or {}
    pipeline = payload.get("report_pipeline") or {}
    return str(meta.get("schema_version") or pipeline.get("version") or "legacy").removeprefix(
        "report-kit-v"
    )


def _download_status(payload):
    qualification = payload.get("report_qualification") or {}
    pipeline = payload.get("report_pipeline") or {}
    verdict = qualification.get("verdict")
    meta = (payload.get("report_v2") or {}).get("meta") or {}
    schema_version = meta.get("schema_version")
    declared_v8 = str(pipeline.get("version") or "").removeprefix("report-kit-v") == "8"
    is_current = declared_v8 or schema_version in {"5", "8"}
    status = pipeline.get("status")
    blocked = verdict in {"hold", "watch"} or status in {"hold", "watch", "manual"}
    document_missing = declared_v8 and schema_version != "8"
    if is_current and (pipeline.get("passed") is not True or document_missing):
        blocked = True
        if status not in LABELS:
            status = "manual"
    if blocked:
        outcome = verdict if verdict in {"hold", "watch"} else status or "manual"
        if outcome == "manual" and (
            pipeline.get("issue_kind") == "model_output"
            or document_missing and any(
                re.search(r"(?:^|: )\d+ validation errors? for \w+\n", str(issue))
                and not re.search(r"type=(?:value_error|assertion_error)", str(issue))
                for issue in pipeline.get("issues") or []
            )
        ):
            return {
                "code": "retry",
                "downloadable": False,
                "label": "Не удалось разобрать ответ модели",
                "detail": "Повторите сборку отчёта. Сервис использует сохранённые материалы.",
                "retryable": True,
            }
        reasons = qualification.get("reasons") or qualification.get("manual_checks") or []
        if outcome == "manual" and document_missing and pipeline.get("passed") is True:
            reasons = ["В сохранённой версии отсутствует документ V8. Пересоберите отчёт."]
        research = payload.get("report_research") or {}
        if research.get("completed") is False and any(
            error.get("stage") == "search" for error in research.get("errors", [])
            if isinstance(error, dict)
        ):
            return {
                "code": "partial",
                "downloadable": False,
                "label": "Поиск завершён частично",
                "detail": (
                    "Материалы сохранены, но часть поисковых запросов не выполнена. "
                    "Продолжите исследование перед итоговым выводом об объекте."
                ),
            }
        return {
            "code": outcome if outcome in LABELS else "manual",
            "downloadable": False,
            "label": LABELS.get(outcome, LABELS["manual"]),
            "detail": " · ".join(REASONS.get(str(x), str(x)) for x in reasons[:3]),
        }
    return {"code": "ready", "downloadable": True, "label": "Отчёт готов", "detail": ""}


def download_status(payload):
    result = _download_status(payload)
    result.pop("code")
    return result


def internal_download_status(payload):
    """The working card can export a checked observation without a client proposal."""
    status = _download_status(payload)
    if status.pop("code") == "watch":
        from radar.reports.observation_v8 import observation_available

        if observation_available(payload):
            return {**status, "downloadable": True, "observation": True}
    return status


def report_group(payload, *, require_v8):
    if payload is None:
        return {"code": "none", "label": "Разбор не начат"}
    if require_v8 and report_format_version(payload) != "8":
        return {"code": "outdated", "label": "Разбор прежнего формата"}
    status = _download_status(payload)
    return {key: status[key] for key in ("code", "label")}


def require_download(payload):
    status = download_status(payload)
    if not status["downloadable"]:
        raise ValueError(status["label"] + (": " + status["detail"] if status["detail"] else ""))
