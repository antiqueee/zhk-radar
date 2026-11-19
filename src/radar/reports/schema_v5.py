"""V5 report contract, separate from the historical four-page report schema."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from radar.reports.schema import Items, Model, Object, Text

Refs = Items(Annotated[int, Field(ge=1, le=6)], 0, 6)
Tone = Literal["neutral", "alert"]


class AtomicFact(Model):
    text: Text(500)
    date: Text(40) | None
    source_n: int = Field(ge=1, le=6)
    who_says: Text(90)
    kind: Literal["fact", "claim", "quote"]
    verbatim: Text(900)


class Facts(Model):
    facts: Items(AtomicFact, 1, 50)


class Criterion(Model):
    name: Literal["Острота", "Публичность"]
    score: int = Field(ge=1, le=3)
    why: Text(50)


class Priority(Model):
    level: Literal["high", "medium", "low"]
    label: Text(16)
    criteria: Items(Criterion, 2)
    outlook: Text(70)

    @model_validator(mode="after")
    def distinct(self):
        if {item.name for item in self.criteria} != {"Острота", "Публичность"}:
            raise ValueError("Нужны два разных критерия: Острота и Публичность")
        return self


class MetricFact(Model):
    value: Text(9)
    label: Text(65)
    refs: Refs
    tone: Tone


class Event(Model):
    date: Text(11)
    date_iso: Annotated[str, Field(pattern=r"^\d{4}(?:-\d{2}(?:-\d{2})?)?$")]
    text: Text(85)
    refs: Refs
    tone: Tone


class Quote(Model):
    text: Text(110)
    attribution: Text(70)
    refs: Refs


class Point(Model):
    text: Text(130)
    refs: Refs


class Buyers(Model):
    status: Text(30)
    note: Text(130)
    note_refs: Refs = Field(default_factory=list)
    quotes: Items(Quote, 0, 2)


class Company(Model):
    title: Text(22) = "Что говорит компания"
    status: Text(30)
    points: Items(Point, 0, 2)


class Situation(Model):
    lead: Text(260)
    facts: Items(MetricFact, 4)
    timeline: Items(Event, 0, 6)
    buyers: Buyers
    company: Company
    gap: Text(46)

    @model_validator(mode="after")
    def timeline_is_evidence(self):
        if 0 < len(self.timeline) < 3:
            raise ValueError("Хронология содержит либо 0, либо 3–6 подтверждённых событий")
        return self


class StageLabel(Model):
    n: int = Field(ge=1, le=4)
    name: Text(30)
    desc: Text(95)


class Sign(Model):
    text: Text(85)
    confirmed: bool
    refs: Refs


class Stage(Model):
    current: int = Field(ge=1, le=4)
    confidence: Text(60)
    stages: Items(StageLabel, 4)
    signs: Items(Sign, 2, 3)
    forecast: Text(200)


class Cause(Model):
    text: Text(70)
    refs: Items(Annotated[int, Field(ge=1, le=6)], 1, 6)


class MainProblem(Model):
    name: Text(22)
    score: int = Field(ge=0, le=10)
    max: int = Field(default=10, ge=1, le=10)
    level: Text(14)
    tone: Literal["high", "medium", "low"]
    scale_note: Text(60)
    causes: Items(Cause, 3, 4)
    trend: Literal["up", "flat", "down"]
    trend_label: Text(20)
    forecast: Text(170)

    @model_validator(mode="after")
    def scale(self):
        if self.score > self.max:
            raise ValueError("Оценка не может превышать максимум шкалы")
        return self


class Source(Model):
    n: int = Field(ge=1, le=6)
    publisher: Text(20)
    date: Text(22)
    title: Text(100)
    type: Literal[
        "публикация", "отзывы", "карточка объекта", "чат", "канал", "сайт дольщиков", "ведомство"
    ]
    url: str | None
    says: Text(110)
    count: int | None = Field(default=None, ge=5)


class Stakeholder(Model):
    who: Text(24)
    cares: Text(60)


class Unknown(Model):
    what: Text(24)
    how: Text(65)


class Diagnosis(Model):
    subtitle: Text(100)
    stage: Stage
    main_problem: MainProblem
    stakeholders: Items(Stakeholder, 3, 4)
    unknowns: Items(Unknown, 3)
    sources: Items(Source, 1, 6)
    monitor_note: Text(70)


class Assessment(Model):
    priority: Priority
    diagnosis: Diagnosis


class Analysis(Assessment):
    situation: Situation


class Risk(Model):
    title: Text(36)
    text: Text(70)
    refs: Refs


class ScenarioInput(Model):
    """Only these scenario details are authored; the tree is assembled by code."""

    triggers: Items(Risk, 3)
    risk_titles: dict[str, Items(Text(60), 3)]
    takeaway: Text(190)
    subtitle: Text(100)


class Exit(Model):
    label: Text(40)
    title: Text(40)
    points: Items(Text(70), 3, 5)
    result: Text(140)
    term: Text(20)
    cost: int = Field(ge=1, le=5)


class ScenarioLevel(Model):
    n: Text(5)
    name: Text(40)
    tag: Text(30)
    desc: Text(100)
    risks_label: Text(30)
    risks: Items(Risk, 3)
    exit: Exit


class PriceStep(Model):
    label: Text(24)
    name: Text(40)
    level: int = Field(ge=1, le=5)


class Price(Model):
    steps: Items(PriceStep, 3)
    takeaway: Text(190)
    note: Text(160)


class Scenario(Model):
    subtitle: Text(100)
    passed: Text(80)
    levels: Items(ScenarioLevel, 3)
    price: Price


class Why(Model):
    title: Text(30)
    text: Text(115)


class Variant(Model):
    name: Text(18)
    text: Text(75)


class Module(Model):
    name: Text(26)
    service: Text(55)
    service_ids: Items(str, 0, 2) = Field(default_factory=list)
    method_ids: Items(str, 0, 2) = Field(default_factory=list)
    pain: Text(100)
    pain_refs: Items(Annotated[int, Field(ge=1, le=6)], 1, 6)
    solution: Text(150)
    agency: Items(Text(60), 3, 4)
    developer: Items(Text(50), 2, 3)
    variants: Items(Variant, 3)
    note: Text(80) | None = None
    outcome: Text(90)
    metric: Text(70)


class Column(Model):
    name: Text(24)
    points: Items(Text(45), 3, 4)


class Road(Model):
    when: Text(20)
    name: Text(24)
    text: Text(70)


class Pilot(Model):
    title: Literal["Первые 30 дней"]
    columns: Items(Column, 3)
    roadmap: Items(Road, 3)

    @model_validator(mode="after")
    def counts(self):
        if [len(c.points) for c in self.columns] != [4, 3, 3]:
            raise ValueError("offer.pilot.columns: число пунктов 4/3/3")
        return self


class Offer(Model):
    subtitle: Text(30)
    usp: Text(180)
    usp_note: Text(120)
    why_now: Items(Why, 3)
    modules: Items(Module, 2, 4)
    pilot: Pilot
    caveat: Text(170)


class Meta(Model):
    schema_version: Literal["5"] = "5"
    product: Text(40)
    doc_type: Text(30)
    date: Text(10)
    disclaimer: Literal["Конфиденциально · Мегаполис"]
    layout: Literal["full", "compact"]
    client_role: Literal["застройщик", "заказчик"]


class ReportV5(Analysis):
    meta: Meta
    object: Object
    evidence_summary: Text(100)
    scenario: Scenario
    offer: Offer


# The final contract is also exported under the conventional pipeline name.
Report = ReportV5
