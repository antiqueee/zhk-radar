"""Strict report contract; short copy is authored, never clipped by the renderer."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def Text(limit):
    return Annotated[str, Field(max_length=limit)]


def Items(item, minimum, maximum=None):
    return Annotated[list[item], Field(min_length=minimum, max_length=maximum or minimum)]


Refs = Items(Annotated[int, Field(ge=1, le=5)], 0, 5)
Tone = Literal["neutral", "alert"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AtomicFact(Model):
    text: Text(500)
    date: Text(40) | None
    source_n: int = Field(ge=1, le=5)
    who_says: Text(90)
    kind: Literal["fact", "claim", "quote"]
    verbatim: Text(900)


class Facts(Model):
    facts: Items(AtomicFact, 1, 35)


class TextEdit(Model):
    path: str
    text: str


class TextEdits(Model):
    edits: Items(TextEdit, 1, 40)


class Criterion(Model):
    name: Literal["Острота", "Публичность", "Окно для захода"]
    score: int = Field(ge=1, le=3)
    why: Text(65)


class Priority(Model):
    level: Literal["high", "medium", "low"]
    label: Text(16)
    criteria: Items(Criterion, 3)


class MetricFact(Model):
    value: Text(8)
    label: Text(65)
    refs: Refs
    tone: Tone


class Event(Model):
    date: Text(10)
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
    quotes: Items(Quote, 0, 2)


class Company(Model):
    status: Text(30)
    points: Items(Point, 1, 2)


class Situation(Model):
    lead: Text(260)
    facts: Items(MetricFact, 4)
    # Sparse evidence must not be padded with fictional events.
    timeline: Items(Event, 0, 6)
    buyers: Buyers
    company: Company
    gap: Text(40)


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


class Driver(Model):
    title: Text(50)
    text: Text(160)
    confidence: Literal["confirmed", "single", "hypothesis"]
    confidence_label: Text(24)
    refs: Refs


class Stakeholder(Model):
    who: Text(24)
    cares: Text(75)


class Unknown(Model):
    what: Text(32)
    how: Text(75)


class Source(Model):
    n: int = Field(ge=1, le=5)
    publisher: Text(45)
    date: Text(24)
    title: Text(70)
    type: Text(25)
    url: str | None


class Diagnosis(Model):
    subtitle: Text(100)
    stage: Stage
    drivers: Items(Driver, 0, 4)
    stakeholders: Items(Stakeholder, 3, 4)
    unknowns: Items(Unknown, 3, 4)
    sources: Items(Source, 1, 5)
    sources_note: Text(150)


class Assessment(Model):
    priority: Priority
    diagnosis: Diagnosis


class Analysis(Model):
    priority: Priority
    situation: Situation
    diagnosis: Diagnosis


class Why(Model):
    title: Text(30)
    text: Text(115)


class Module(Model):
    name: Text(22)
    service: Text(55)
    service_ids: Items(str, 1, 2)
    method_ids: Items(str, 0, 2)
    pain: Text(75)
    pain_refs: Items(int, 1, 5)
    solution: Text(160)
    steps: Items(Text(78), 3)
    outcome: Text(90)
    metric: Text(75)


class Offer(Model):
    usp: Text(180)
    usp_note: Text(120)
    why_now: Items(Why, 3)
    modules: Items(Module, 3)


class Contact(Model):
    role: Text(34)
    pain: Text(55)
    angle: Text(60)


class Message(Model):
    greeting: Text(40)
    paragraphs: Items(Text(400), 3)
    tips: Items(Text(70), 3)

    @model_validator(mode="after")
    def total(self):
        if sum(map(len, self.paragraphs)) > 520:
            raise ValueError("message.paragraphs: максимум 520 знаков суммарно")
        return self


class Objection(Model):
    q: Text(30)
    a: Text(150)


class Column(Model):
    name: Text(24)
    points: Items(Text(52), 3, 5)


class Road(Model):
    when: Text(20)
    name: Text(24)
    text: Text(70)


class Pilot(Model):
    title: Text(40)
    columns: Items(Column, 3)
    roadmap: Items(Road, 3)

    @model_validator(mode="after")
    def counts(self):
        if [len(c.points) for c in self.columns] != [5, 3, 3]:
            raise ValueError("pilot.columns: число пунктов 5/3/3")
        return self


class Approach(Model):
    contacts: Items(Contact, 3)
    contacts_note: Text(130)
    message: Message
    objections: Items(Objection, 3)
    pilot: Pilot
    checklist: Items(Text(62), 4)
    caveat: Text(170)


class Meta(Model):
    product: Text(40)
    doc_type: Text(30)
    date: Text(10)
    disclaimer: Text(90)
    layout: Literal["full", "compact"]


class Object(Model):
    name: Text(60)
    short_name: Text(60)
    developer: Text(65)
    city: Text(60)


class Report(Analysis):
    meta: Meta
    object: Object
    evidence_summary: Text(100)
    offer: Offer
    approach: Approach


class Check(Model):
    criterion: int = Field(ge=1, le=9)
    passed: bool
    quote: Text(220)
    reason: Text(350)


class Critique(Model):
    checks: Items(Check, 9)

    @model_validator(mode="after")
    def unique(self):
        if {c.criterion for c in self.checks} != set(range(1, 10)):
            raise ValueError("Критик должен проверить все девять критериев")
        if any(not c.passed and not c.reason for c in self.checks):
            raise ValueError("Не пройденный критерий требует объяснения")
        return self

    @property
    def passed(self):
        return all(c.passed for c in self.checks)
