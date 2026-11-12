"""Deterministic offer selection; preview supports unapproved library drafts."""

from pathlib import Path

import yaml
from pydantic import Field, model_validator

from radar.domain import StrictModel


class Offer(StrictModel):
    code: str
    name: str
    stages: list[int]
    situation_types: list[str]
    pain_topics: list[str]
    promise: str
    deliverables: list[str]
    pilot_30d: str
    kpis: list[str]
    buyer_roles: list[str]
    not_when: list[str]
    source_pages: list[int]
    practice_sources: list[str] = Field(default_factory=list)
    approved: bool = False
    supplementary_only: bool = False


class OfferLibrary(StrictModel):
    version: int
    status: str
    source: str
    packaging_note: str
    offers: list[Offer]

    @model_validator(mode="after")
    def unique_codes(self):
        codes = [o.code for o in self.offers]
        if len(codes) != len(set(codes)) or "O1" not in codes:
            raise ValueError("Нужны уникальные коды и диагностический оффер O1")
        return self


class PainWeight(StrictModel):
    topic: str
    weight: float = Field(gt=0, allow_inf_nan=False)


def load_offers(path: Path = Path("config/offers.yaml")) -> OfferLibrary:
    return OfferLibrary.model_validate(yaml.safe_load(path.read_text()))


def select_offers(
    library: OfferLibrary,
    pains: list[PainWeight],
    stage: int,
    situation: str,
    chats_connected: bool,
    low_confidence: bool,
    blockers: set[str],
    *,
    preview: bool = False,
) -> list[Offer]:
    if stage not in range(5):
        raise ValueError("Стадия должна быть от 0 до 4")
    candidates = [
        o
        for o in library.offers
        if stage in o.stages
        and situation in o.situation_types
        and not blockers.intersection(o.not_when)
        and (preview or o.approved)
    ]
    weights = {}
    for p in pains:
        weights[p.topic] = weights.get(p.topic, 0) + p.weight
    ranked = sorted(
        candidates,
        key=lambda o: (
            o.supplementary_only,
            -sum(weights.get(t, 0) for t in set(o.pain_topics)),
            o.code,
        ),
    )
    diagnosis = next((o for o in candidates if o.code == "O1"), None)
    # Missing chat coverage limits evidence; it is not a reason to sell an audit.
    relevant = [
        o for o in ranked if not o.supplementary_only and set(o.pain_topics) & weights.keys()
    ]
    selected = relevant[:1] or ([diagnosis] if diagnosis else [])
    if not selected:
        return []
    covered = set(selected[0].pain_topics)
    for offer in ranked:
        if len(selected) >= 3:
            break
        if offer in selected or offer.code == "O1":
            continue
        if (set(offer.pain_topics) & weights.keys()) - covered:
            selected.append(offer)
            covered.update(offer.pain_topics)
    return selected
