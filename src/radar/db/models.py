import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Numeric, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Identity:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)


class Developer(Identity, Base):
    __tablename__ = "developers"
    name: Mapped[str]
    aliases: Mapped[list] = mapped_column(JSONB, default=list)
    state: Mapped[str] = mapped_column(default="unknown")


class Complex(Identity, Base):
    __tablename__ = "complexes"
    developer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("developers.id"))
    name: Mapped[str]
    region_code: Mapped[str]
    aliases: Mapped[list] = mapped_column(JSONB, default=list)


class Object(Identity, Base):
    __tablename__ = "objects"
    __table_args__ = (
        CheckConstraint("readiness_pct BETWEEN 0 AND 100"),
        CheckConstraint("flats_count >= 0"),
    )
    complex_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("complexes.id"))
    eisgs_id: Mapped[str] = mapped_column(unique=True)
    label: Mapped[str]
    flats_count: Mapped[int | None]
    commissioning_initial: Mapped[date | None]
    commissioning_current: Mapped[date | None]
    transfer_initial: Mapped[date | None]
    transfer_current: Mapped[date | None]
    rve_date: Mapped[date | None]
    readiness_pct: Mapped[float | None]
    n_commissioning_shifts: Mapped[int] = mapped_column(default=0)
    n_transfer_shifts: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(default="new")
    assessment: Mapped[dict] = mapped_column(JSONB, default=dict)


class ObjectSnapshot(Identity, Base):
    __tablename__ = "object_snapshots"
    __table_args__ = (UniqueConstraint("object_id", "taken_at", "source"),)
    object_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("objects.id"))
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str]
    readiness_pct: Mapped[float | None]
    commissioning: Mapped[date | None]
    transfer: Mapped[date | None]
    payload: Mapped[dict] = mapped_column(JSONB)


class Run(Identity, Base):
    __tablename__ = "runs"
    kind: Mapped[str]
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(default="running")
    stats: Mapped[dict] = mapped_column(JSONB, default=dict)
    cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), default=0)
    error_code: Mapped[str | None]


class Source(Identity, Base):
    __tablename__ = "sources"
    __table_args__ = (CheckConstraint("trust_class IN ('A','B','C')"),)
    kind: Mapped[str]
    name: Mapped[str]
    base_url: Mapped[str]
    trust_class: Mapped[str]
    active: Mapped[bool] = mapped_column(default=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fail_streak: Mapped[int] = mapped_column(default=0)


class Document(Identity, Base):
    __tablename__ = "documents"
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id"))
    url: Mapped[str]
    url_hash: Mapped[str] = mapped_column(unique=True)
    title: Mapped[str]
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    text: Mapped[str] = mapped_column(Text)
    text_hash: Mapped[str]
    raw_path: Mapped[str]


class Signal(Identity, Base):
    __tablename__ = "signals"
    __table_args__ = (
        CheckConstraint("length(trim(evidence_quote)) > 0"),
        CheckConstraint("trust_class IN ('A','B','C')"),
        CheckConstraint(
            "(level = 'object' AND object_id IS NOT NULL AND complex_id IS NULL "
            "AND developer_id IS NULL) OR "
            "(level = 'complex' AND object_id IS NULL AND complex_id IS NOT NULL "
            "AND developer_id IS NULL) OR "
            "(level = 'developer' AND object_id IS NULL AND complex_id IS NULL "
            "AND developer_id IS NOT NULL)"
        ),
    )
    level: Mapped[str]
    object_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("objects.id"))
    complex_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("complexes.id"))
    developer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("developers.id"))
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"))
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"))
    kind: Mapped[str]
    occurred_at: Mapped[date]
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    summary: Mapped[str]
    evidence_quote: Mapped[str] = mapped_column(Text)
    trust_class: Mapped[str]
    origin: Mapped[str]
    verification: Mapped[str] = mapped_column(default="auto")
    dedup_key: Mapped[str] = mapped_column(unique=True)


class SignalDocument(Base):
    """Keep all reprints as evidence without multiplying signal scores."""

    __tablename__ = "signal_documents"
    signal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("signals.id"), primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), primary_key=True)
    evidence_quote: Mapped[str] = mapped_column(Text)
