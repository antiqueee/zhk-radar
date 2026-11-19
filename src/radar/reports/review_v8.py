"""Ground review claims in the authored report before repairing any page.

This module makes no model calls. A bad review is re-read under a bounded review
budget by the caller; it never becomes an approval or an instruction to the author.
"""

import json
import re
from typing import Literal

from pydantic import Field, create_model, model_validator

from radar.reports import schema
from radar.web.chats import redact


class ReviewCheck(schema.Check):
    quote: str
    reason: str
    path: str | None = None


class ReviewCritique(schema.Critique):
    checks: schema.Items(ReviewCheck, 9)


PROMPT_NATIVE = """Верни все девять checks по прежним критериям, не меняя их смысл.
Этот формат заменяет любые прежние указания самостоятельно писать quote/path:
возвращай только field_key, а дословную quote и path вставит программа.
Для каждого check: criterion, passed, field_key, reason. Не пиши quote или path.
report_field_options — единственный реестр разрешённых field_key: скопируй ключ целиком.
Выбирай поле только когда criterion входит в его allowed_criteria.
При passed=false field_key обязателен; при passed=true допустим null.
Программа сама подставит дословный текст всего выбранного поля report и его path.
Факты, источники и каталог нужны для проверки, но их нельзя выбирать вместо поля report.
Если обнаружена фактическая ошибка, выбери поле report, в котором она допущена;
не указывай поле из facts или каталога. Не исправляй и не пересказывай цитату сам.
Описания шкалы стадий и шаблон сценариев задаются конфигом. Для замечания о текущей стадии
выбирай утверждение отчёта в confidence, signs или forecast. При проверке сценария выбирай
авторский подзаголовок, триггер текущей стадии или вывод, где допущена ошибка.
Если не подходит выбранная карточка риска будущей стадии, укажи её поле и запроси выбор
другой карточки из разрешённого списка. Текст самой карточки не переписывай.
Предмет критерия остаётся строгим: 3 — offer.modules; 4 — предложение/решения агентства,
не фактический why_now; 5 — различие решений в modules; 9 — роли/границы работы
в modules/pilot/usp_note/caveat. 7 — результат в offer/scenario/diagnosis.
1,2,6,8 — подходящие видимые поля отчёта. У каждого reason конкретное объяснение."""


class ReviewGroundingError(ValueError):
    def __init__(self, issues):
        self.feedback = {
            "kind": "critic_grounding_error",
            "issues": issues,
            "instruction": (
                "Повторно проверь тот же неизменённый report по всем девяти критериям. "
                "Исправь собственные цитаты/path: копируй из конкретного строкового поля report, "
                "не из facts, каталога или контекста. Не меняй passed ради формата. "
                "Отчёт нельзя исправлять по неподтверждённому замечанию критика."
            ),
        }
        super().__init__(json.dumps(self.feedback, ensure_ascii=False))


def _data(value):
    return value.model_dump() if hasattr(value, "model_dump") else value


def _leaves(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            yield from _leaves(value, path + "/" + escaped)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _leaves(value, path + "/" + str(index))
    elif isinstance(node, str):
        yield path, node


def _parts(path):
    if not isinstance(path, str) or not path.startswith("/") or re.search(r"~(?![01])", path):
        return []
    return [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]


def _visible(path):
    parts = _parts(path)
    return bool(
        parts
        and parts[0] != "meta"
        and parts[-1] not in {"url", "move", "service_ids", "method_ids"}
    )


def _review_target(path):
    parts = _parts(path)
    if not _visible(path) or {"service_ids", "method_ids"}.intersection(parts):
        return False
    if parts[:3] == ["diagnosis", "stage", "stages"] or parts == ["offer", "subtitle"]:
        return False
    if parts[0] == "scenario":
        # Fixed tree and exits come from PLAYBOOK; the author can reselect future risks.
        return (
            parts in (["scenario", "subtitle"], ["scenario", "price", "takeaway"])
            or (len(parts) == 6 and parts[1] == "levels" and parts[2] in {"0", "1", "2"}
                and parts[3] == "risks"
                and parts[-1] in {"title", "text"})
        )
    return True


def field_options(report):
    """One deterministic key for each nonempty selectable field of this report."""
    leaves = sorted(_leaves(_data(report)))
    return [
        {"field_key": f"r{index:04d}", "path": path, "text": text,
         "allowed_criteria": [n for n in range(1, 10) if _scope(n, path)]}
        for index, (path, text) in enumerate(
            ((path, text) for path, text in leaves if text.strip() and _review_target(path)),
            1,
        )
    ]


def native_contract(options):
    """The critic chooses a field identity, never independently authored quote/path."""
    keys = tuple(row["field_key"] for row in options)
    if len(set(keys)) != len(keys):
        raise ValueError("Реестр полей отчёта содержит повторный ключ")
    key_type = Literal[keys] | None if keys else type(None)

    def failed_requires_field(self):
        if not self.passed and self.field_key is None:
            raise ValueError("Непройденный критерий требует field_key поля report")
        return self

    check = create_model(
        "NativeReviewCheck", __base__=schema.Model,
        __validators__={"failed_requires_field": model_validator(mode="after")(
            failed_requires_field
        )},
        criterion=(int, Field(ge=1, le=9)),
        passed=(bool, ...),
        field_key=(key_type, ...),
        reason=(str, ...),
    )
    # Retain Critique.unique and passed: exactly the same nine criterion identities.
    return create_model(
        "NativeReviewCritique", __base__=schema.Critique,
        checks=(schema.Items(check, 9), ...),
    )


def resolve_native(draft, options, report):
    """Resolve exact snapshot fields, then run the existing strict review grounding."""
    if options != field_options(report):
        raise ReviewGroundingError([{
            "reason": "Реестр field_key не соответствует текущему неизменённому report"
        }])
    value = native_contract(options).model_validate(_data(draft))
    by_key = {row["field_key"]: row for row in options}
    checks = []
    for check in value.checks:
        selected = by_key[check.field_key] if check.field_key is not None else None
        # An approval does not require a field. A failed verdict still needs an
        # in-scope field, as enforced below by validate_critique.
        if check.passed and selected and check.criterion not in selected["allowed_criteria"]:
            selected = None
        checks.append({
            "criterion": check.criterion, "passed": check.passed, "reason": check.reason,
            "path": selected["path"] if selected else None,
            "quote": selected["text"] if selected else "",
        })
    return validate_critique({"checks": checks}, report)


def _scope(criterion, path):
    parts = _parts(path)
    if not parts or parts[0] == "meta" or parts[-1] == "url":
        return False
    if criterion in {1, 2, 6, 8}:
        return True
    if criterion == 3:
        return parts[:2] == ["offer", "modules"]
    if criterion in {4, 5, 9}:
        if parts[:2] == ["offer", "modules"] and len(parts) > 3:
            return criterion != 4 or parts[3] in {
                "name",
                "solution",
                "agency",
                "outcome",
                "variants",
                "note",
            }
        if criterion == 4:
            return parts[:2] in (["offer", "usp"], ["offer", "usp_note"], ["offer", "pilot"])
        return criterion == 9 and parts[:2] in (
            ["offer", "pilot"],
            ["offer", "usp_note"],
            ["offer", "caveat"],
        )
    return criterion == 7 and parts[0] in {"offer", "scenario", "diagnosis"}


def validate_critique(critique, report):
    """Resolve a report JSON pointer without changing any passed/failed decision.

    Old Check records can omit path only when their exact quote has one eligible
    location. Every nonempty quote, including an approving one, must be real.
    """
    value = ReviewCritique.model_validate(_data(critique))
    leaves = dict(_leaves(_data(report)))
    issues = []
    for check in value.checks:
        prefix = {"criterion": check.criterion, "path": check.path, "quote": check.quote}
        if not check.reason.strip():
            issues.append({**prefix, "reason": "Нужно конкретно указать, что проверено/исправить"})
        if not check.quote:
            if not check.passed:
                issues.append(
                    {**prefix, "reason": "Непройденный критерий требует цитаты из report"}
                )
            elif check.path:
                issues.append({**prefix, "reason": "У пустой цитаты не должно быть path"})
            continue
        if check.path:
            text = leaves.get(check.path)
            if not isinstance(text, str) or check.quote not in text:
                issues.append(
                    {**prefix, "reason": "Цитата отсутствует в указанном строковом поле report"}
                )
            elif not _scope(check.criterion, check.path):
                issues.append({**prefix, "reason": "Поле не относится к предмету этого критерия"})
            elif not check.passed and not _review_target(check.path):
                issues.append({
                    **prefix, "reason": "Это поле не может быть исправлено автором отчёта",
                })
        else:
            candidates = [
                path
                for path, text in leaves.items()
                if check.quote in text and _scope(check.criterion, path) and _visible(path)
                and (check.passed or _review_target(path))
            ]
            if len(candidates) != 1:
                issues.append(
                    {
                        **prefix,
                        "reason": (
                            "Цитата должна однозначно указывать поле report; укажи точный path"
                        ),
                        "candidate_paths": candidates[:6],
                    }
                )
            else:
                check.path = candidates[0]
    if issues:
        raise ReviewGroundingError(issues)
    return value


def repair_feedback(critique, report):
    """Only validated failures can become page-author feedback."""
    value = validate_critique(critique, report)
    owners = {
        "situation": "situation",
        "diagnosis": "assessment",
        "priority": "assessment",
        "scenario": "scenario",
        "offer": "offer",
        "object": "identity",
    }
    issues = []
    for check in value.checks:
        if check.passed:
            continue
        page = owners.get(_parts(check.path)[0], "identity")
        issues.append(
            {
                "criterion": check.criterion,
                "page": page,
                "path": check.path,
                "quote": check.quote,
                "reason": check.reason,
            }
        )
    return {
        "issues": issues,
        "pages": list(dict.fromkeys(item["page"] for item in issues)),
        "instruction": (
            "Исправь только перечисленные поля/страницы. Сохрани остальные факты и ссылки. "
            "Удали неподтверждённый вывод целиком, даже если его можно перефразировать. "
            "В обещаниях оставь работу и результат, которыми агентство управляет."
        ),
    }


def compact_review_context(report, context):
    """Keep source proof and used catalogue identities, never unused model examples."""
    report, context = _data(report), _data(context)
    modules = report.get("offer", {}).get("modules", [])
    service_ids = {x for module in modules for x in module.get("service_ids", [])}
    method_ids = {x for module in modules for x in module.get("method_ids", [])}
    services = {module["service"] for module in modules}
    moves = {module["move"] for module in modules}
    facts = _data(context.get("facts") or {})
    facts = facts.get("facts", []) if isinstance(facts, dict) else facts
    names = context.get("private_names") or []
    # A cited-source filter would hide contrary evidence or an omitted company
    # explanation from the critic. All already-scoped PDF facts remain available.
    proof = [
        {
            key: redact(str(fact[key]), names) if key in {"text", "verbatim"} else fact[key]
            for key in ("text", "date", "source_n", "kind", "verbatim")
            if key in fact
        }
        for raw in facts
        if isinstance(fact := _data(raw), dict)
    ]
    used_catalog = [
        {
            key: row[key]
            for key in ("id", "name", "purpose", "description", "promise", "deliverables")
            if key in row
        }
        for row in context.get("services", [])
        if row.get("id") in service_ids or row.get("name") in services
    ]
    methods = [
        {
            key: row[key]
            for key in ("id", "name", "when", "result", "measurement", "client_inputs")
            if key in row
        }
        for row in context.get("methods", [])
        if row.get("id") in method_ids
    ]
    source_fields = {
        "n",
        "title",
        "publisher",
        "date",
        "display_date",
        "evidence_date",
        "published_at",
        "date_provenance",
        "type",
        "contour",
        "kind",
        "source_kind",
        "url",
        "count",
    }
    sources = [
        {key: value for key, value in row.items() if key in source_fields}
        for row in context.get("sources", [])
    ]
    qualification = context.get("qualification") or {}
    result = {
        "report": report,
        "as_of": context.get("as_of"),
        "facts": {"facts": proof},
        "sources": sources,
        "qualification": {
            key: qualification[key]
            for key in ("passport", "current_stage", "stage_signs", "problem_registry")
            if key in qualification
        },
        "focus": context.get("focus"),
        "monitoring_scale": context.get("monitoring_scale"),
        "current_monitoring_score": context.get("current_monitoring_score"),
        "report_scope": context.get("report_scope"),
        "stage_labels": context.get("stage_labels"),
        "allowed_risks": context.get("allowed_risks"),
        "text_rules": context.get("text_rules"),
        "services": used_catalog,
        "methods": methods,
        "products": {
            key: value
            for key, value in (context.get("products") or {}).get("products", {}).items()
            if value in services
        },
        "allowed_moves": sorted(
            {
                row["move"]
                for key in ("picked", "reserve")
                for row in (context.get("matrix_selection") or {}).get(key, [])
            }
        ),
        "used_move_conditions": [
            {key: row[key] for key in ("move", "service", "limits") if key in row}
            for row in context.get("matrix_cards", [])
            if row["move"] in moves
        ],
    }
    if any(module["move"] == "I01" for module in modules):
        result["public_communities"] = context.get("public_communities") or []
    return result
