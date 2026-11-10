from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class House(StrictModel):
    eisgs_id: str = Field(min_length=1)
    label: str
    flats: int | None = Field(default=None, ge=0)
    commissioning_initial: date | None = None
    commissioning: date | None = None
    transfer_initial: date | None = None
    transfer: date | None = None
    rve: date | None = None
    readiness: float | None = Field(default=None, ge=0, le=100)
    commissioning_shifts: int = Field(default=0, ge=0)
    transfer_shifts: int = Field(default=0, ge=0)
    delayed_share: float | None = Field(default=None, ge=0, le=1)


class Snapshot(StrictModel):
    on: date
    readiness: float | None = Field(default=None, ge=0, le=100)


class Evidence(StrictModel):
    kind: str
    occurred_at: date
    trust: Literal["A", "B", "C"]
    # Same origin for reprints; independence cannot be inferred from domains alone.
    origin: str = Field(min_length=1)
    quote: str = Field(min_length=1)
    document_id: str = Field(min_length=1)


class Sales(StrictModel):
    state: Literal["active", "bankrupt", "third_party", "disputed", "unknown"] = "unknown"
    solvent: bool = False
    buyer_known: bool = False
    public_response: bool = False
    demand: bool = False
    other_objects: int = Field(default=0, ge=0)
    warm_contact: bool = False
    other_agency: bool = False
    stoplisted: bool = False


class Assessment(StrictModel):
    severity: int
    sellability: int
    window: int
    confidence: Literal["high", "medium", "low"]
    priority: Literal["A", "B", "C", "early", "watch", "none"]
    early_rules: list[str]
    eligible: bool
    contributions: dict[str, float]
    limitations: list[str]
