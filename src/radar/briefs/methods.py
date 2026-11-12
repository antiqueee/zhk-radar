"""Bounded local retrieval of reviewed agency methods, separate from factual evidence."""

import hashlib
import json
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator

from radar.domain import StrictModel

LIBRARY_PATH = Path(__file__).with_name("agency_methods.json")
MAX_METHODS = 6
MAX_CONTEXT_CHARS = 14000


class Reference(StrictModel):
    source_id: str
    locator: str


class ArchiveSource(StrictModel):
    id: str
    filename: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    kind: str
    review: str


class Method(StrictModel):
    id: str
    name: str
    offer_codes: list[str]
    triggers: list[str]
    when: str
    steps: list[str] = Field(min_length=3, max_length=3)
    result: str
    measurement: str
    client_inputs: str
    resource_choice: str
    creative_seed: str
    references: list[Reference] = Field(min_length=1)


class Library(StrictModel):
    version: int
    sources: list[ArchiveSource]
    methods: list[Method]

    @model_validator(mode="after")
    def check_references(self):
        ids = {s.id for s in self.sources}
        if len(ids) != len(self.sources) or len({m.id for m in self.methods}) != len(self.methods):
            raise ValueError("Повтор идентификатора в методической базе")
        if any(r.source_id not in ids for m in self.methods for r in m.references):
            raise ValueError("Неизвестный источник методики")
        return self


@lru_cache(maxsize=4)
def _parse(raw: bytes) -> Library:
    return Library.model_validate_json(raw)


def normalize(text):
    return text.casefold().replace("ё", "е")


def select(sources, products=(), question=""):
    """Only supplied evidence/question enters matching; old cases and boilerplate never do."""
    raw = LIBRARY_PATH.read_bytes()
    library = _parse(raw)
    text = normalize("\n".join(s.get("text", "") for s in sources))
    question = normalize(question)
    ranked = []
    for method in library.methods:
        evidence_hits = sum(normalize(t) in text for t in method.triggers)
        question_hits = sum(normalize(t) in question for t in method.triggers)
        if evidence_hits or question_hits:
            # Question adds a scenario, not proof that this condition actually exists.
            ranked.append((method, evidence_hits, question_hits))
    # Portfolio coordination is supplementary, never displaces a concrete problem's method.
    ranked.sort(
        key=lambda item: (item[0].id == "M15", -min(item[1], 4), -min(item[2], 3), item[0].id)
    )
    chosen = []
    if question:
        scenario = next((m for m, _, hits in ranked if hits), None)
        if scenario:
            chosen.append(scenario)
    # Cover each offered programme before filling remaining slots, without forcing a match.
    for product in products:
        candidate = next((m for m, _, _ in ranked if product["code"] in m.offer_codes), None)
        if candidate and candidate not in chosen:
            chosen.append(candidate)
    chosen += [m for m, _, _ in ranked if m not in chosen]
    context = {
        "version": library.version,
        "fingerprint": hashlib.sha256(raw).hexdigest(),
        "archive_files": len(library.sources),
        "meaning": "Методы из архивных предложений; не факты об объекте и не доказанные кейсы. "
        "Применение условно: проверь when и client_inputs по текущим источникам. "
        "creative_seed — новая идея пилота, не факт выполненной работы агентства.",
        "methods": [],
    }
    for method in chosen:
        if len(context["methods"]) >= MAX_METHODS:
            break
        item = method.model_dump(exclude={"triggers"})
        item["selection_basis"] = (
            "evidence_topic"
            if any(normalize(t) in text for t in method.triggers)
            else "question_only"
        )
        # Archive filenames, prices, people and raw client documents stay local.
        if (
            len(json.dumps({**context, "methods": [*context["methods"], item]}, ensure_ascii=False))
            > MAX_CONTEXT_CHARS
        ):
            continue
        context["methods"].append(item)
    return context


def apply_to_report(report, context):
    """Useful offline fallback; the model subsequently adapts, rather than copies, these steps."""
    used = set()
    for product in report["products"]:
        eligible = [m for m in context["methods"] if product["code"] in m["offer_codes"]]
        product["method_candidates"] = [m["id"] for m in eligible]
        method = next((m for m in eligible if m["id"] not in used), None)
        if method:
            used.add(method["id"])
            product.update(
                tasks=method["steps"],
                result=method["result"],
                measurement=method["measurement"],
                dependency=method["client_inputs"],
                method_ids=[method["id"]],
            )
    return report
