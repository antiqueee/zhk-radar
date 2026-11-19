"""V8 validates evidence and structure; Chromium decides whether copy fits.

Prose has editorial length targets in the prompts, not character-count gates.
Historical V5 and source-extraction contracts keep their original constraints.
"""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from radar.reports import schema_v5 as old
from radar.reports.schema import Items, Model, Text
from radar.reports.schema import Object as OldObject

AtomicFact = old.AtomicFact


class Object(OldObject):
    name: str
    short_name: str
    developer: str
    city: str


class Criterion(old.Criterion):
    why: str


class Priority(old.Priority):
    label: str
    criteria: Items(Criterion, 2)
    outlook: str


class MetricFact(old.MetricFact):
    value: str
    label: str


class Event(old.Event):
    date: str
    date_iso: Annotated[str, Field(pattern=r"^\d{4}(?:-Q[1-4]|-\d{2}(?:-\d{2})?)?$")]
    text: str


class Quote(old.Quote):
    text: str
    attribution: str


class Point(old.Point):
    text: str


class Topic(Model):
    text: str
    refs: Items(int, 1, 6)


class Buyers(old.Buyers):
    status: str
    note: str
    quotes: Items(Quote, 0, 2)
    topics: Items(Topic, 0, 3) = Field(default_factory=list)

    @model_validator(mode="after")
    def real_voice(self):
        if not self.quotes and not self.topics:
            raise ValueError("Нужны подтверждённые цитаты или темы претензий")
        return self


class Company(old.Company):
    title: str = "Что говорит компания"
    status: str
    points: Items(Point, 0, 2)


class Situation(old.Situation):
    lead: str
    facts: Items(MetricFact, 4)
    timeline: Items(Event, 0, 6)
    buyers: Buyers
    company: Company
    gap: str


class StageLabel(old.StageLabel):
    name: str
    desc: str


class Sign(old.Sign):
    text: str


class Stage(old.Stage):
    confidence: str
    stages: Items(StageLabel, 4)
    signs: Items(Sign, 2, 3)
    forecast: str


class Cause(old.Cause):
    text: str


class MainProblem(old.MainProblem):
    name: str
    level: str
    scale_note: str
    causes: Items(Cause, 3, 4)
    trend_label: str
    forecast: str


class Source(old.Source):
    publisher: str
    date: str
    title: str
    says: str


class Stakeholder(old.Stakeholder):
    who: str
    cares: str


class Unknown(old.Unknown):
    what: str
    how: str


class RiskPoint(Model):
    text: str
    refs: Items(int, 1, 6)


class ExtraRisk(Model):
    name: str
    tag: str
    note: str
    points: Items(RiskPoint, 2, 3)
    effect: str


class Diagnosis(old.Diagnosis):
    subtitle: str
    stage: Stage
    main_problem: MainProblem
    stakeholders: Items(Stakeholder, 3, 4)
    unknowns: Items(Unknown, 3)
    sources: Items(Source, 1, 6)
    monitor_note: str
    extra_risk: ExtraRisk | None = None

    @model_validator(mode="after")
    def risk_layout(self):
        if self.extra_risk and (
            len(self.stage.signs) != 2
            or len(self.main_problem.causes) != 3
            or len(self.stakeholders) != 3
        ):
            raise ValueError("С зоной риска нужны 2 признака стадии, 3 причины и 3 группы")
        return self


class Assessment(Model):
    priority: Priority
    diagnosis: Diagnosis


class Analysis(Assessment):
    situation: Situation


class Risk(old.Risk):
    title: str
    text: str


class ScenarioInput(old.ScenarioInput):
    triggers: Items(Risk, 3)
    risk_titles: dict[str, Items(str, 3)]
    takeaway: str
    subtitle: str


class Exit(old.Exit):
    label: str
    title: str
    points: Items(str, 3, 6)
    result: str
    term: str


class ScenarioLevel(old.ScenarioLevel):
    name: str
    tag: str
    desc: str
    risks_label: str
    risks: Items(Risk, 3)
    exit: Exit


class PriceStep(old.PriceStep):
    label: str
    name: str


class Price(old.Price):
    steps: Items(PriceStep, 3)
    takeaway: str
    note: str


class Scenario(old.Scenario):
    subtitle: str
    passed: str
    levels: Items(ScenarioLevel, 3)
    price: Price


class Why(old.Why):
    title: str
    text: str


class Variant(old.Variant):
    name: str
    text: str


class Module(old.Module):
    name: str
    service: str
    move: Text(8)
    pain: str
    solution: str
    agency: Items(str, 3, 6)
    developer: Items(str, 1, 3)
    variants: Items(Variant, 0, 5)
    note: str | None = None
    outcome: str
    metric: str

    @model_validator(mode="after")
    def formats(self):
        if len(self.variants) not in {0, 3, 4, 5}:
            raise ValueError("Варианты: 0 или 3–5")
        return self


class Column(old.Column):
    name: str
    points: Items(str, 3, 4)


class Road(old.Road):
    when: str
    name: str
    text: str


class Pilot(old.Pilot):
    columns: Items(Column, 3)
    roadmap: Items(Road, 3)


class Offer(old.Offer):
    subtitle: str
    usp: str
    usp_note: str
    why_now: Items(Why, 3)
    modules: Items(Module, 2, 4)
    pilot: Pilot
    caveat: str


class Meta(old.Meta):
    schema_version: Literal["8"] = "8"
    product: str
    doc_type: str


class ReportV8(Analysis):
    meta: Meta
    object: Object
    evidence_summary: str
    scenario: Scenario
    offer: Offer


Report = ReportV8
ReportV5 = ReportV8  # Shared engine constructor name; the actual contract is V8.


class ProblemMatch(Model):
    problem_id: Text(60)
    matrix_id: Text(12) | None


class MatrixSignal(Model):
    code: Text(12)
    refs: Items(int, 1, 6)


class MatrixMatch(Model):
    problems: Items(ProblemMatch, 0, 30)
    tactics: Items(MatrixSignal, 0, 20)
    accelerators: Items(MatrixSignal, 0, 20)
    factors: Items(MatrixSignal, 0, 10)
