"""Database tables (spec section 5). SQLite via SQLAlchemy.

All tables are defined now so later phases add behaviour, not schema churn.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_code: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str | None] = mapped_column(String(255))
    versions: Mapped[list["Version"]] = relationship(back_populates="task")


class Version(Base):
    __tablename__ = "versions"
    __table_args__ = (UniqueConstraint("task_id", "label"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"))
    label: Mapped[str] = mapped_column(String(64))
    zip_sha256: Mapped[str] = mapped_column(String(64), index=True)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    task: Mapped[Task] = relationship(back_populates="versions")
    files: Mapped[list["File"]] = relationship(back_populates="version", cascade="all, delete-orphan")
    criteria: Mapped[list["Criterion"]] = relationship(cascade="all, delete-orphan")
    failure_notes: Mapped[list["FailureNote"]] = relationship(cascade="all, delete-orphan")


class File(Base):
    __tablename__ = "files"
    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("versions.id"))
    role: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(512))  # path inside the zip
    sha256: Mapped[str] = mapped_column(String(64))
    text_path: Mapped[str | None] = mapped_column(String(1024))  # extraction JSON under DATA_DIR
    metadata_json: Mapped[str | None] = mapped_column(Text)
    parts_json: Mapped[str | None] = mapped_column(Text)
    version: Mapped[Version] = relationship(back_populates="files")


class Criterion(Base):
    __tablename__ = "criteria"
    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("versions.id"))
    crit_id: Mapped[str] = mapped_column(String(32))
    subtask: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    points: Mapped[float] = mapped_column(Float)
    category: Mapped[str | None] = mapped_column(String(64))
    references_json: Mapped[str | None] = mapped_column(Text)


class FailureNote(Base):
    __tablename__ = "failure_notes"
    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("versions.id"))
    idx: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)


class Report(Base):
    __tablename__ = "reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"))
    flagged_version: Mapped[str | None] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(String(512))
    expected_counts_json: Mapped[str | None] = mapped_column(Text)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Card(Base):
    __tablename__ = "cards"
    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("reports.id"))
    code: Mapped[str] = mapped_column(String(32))
    part: Mapped[str] = mapped_column(String(8))
    lead_row_id: Mapped[int | None] = mapped_column(Integer)


class Row(Base):
    __tablename__ = "rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("cards.id"))
    category_row: Mapped[str | None] = mapped_column(String(64))
    settled_with: Mapped[str | None] = mapped_column(Text)
    theme: Mapped[str | None] = mapped_column(Text)
    where: Mapped[str | None] = mapped_column(Text)
    criteria_list: Mapped[str | None] = mapped_column(Text)
    points_at_stake: Mapped[str | None] = mapped_column(String(64))
    finding_raw: Mapped[str | None] = mapped_column(Text)
    sections_json: Mapped[str | None] = mapped_column(Text)
    defense_raw: Mapped[str | None] = mapped_column(Text)
    qa_comment: Mapped[str | None] = mapped_column(Text)
    expert_call: Mapped[str | None] = mapped_column(Text)
    expert_note: Mapped[str | None] = mapped_column(Text)
    fixed_by: Mapped[str | None] = mapped_column(Text)
    rule: Mapped[str | None] = mapped_column(String(64))
    engine: Mapped[str | None] = mapped_column(String(64))
    also_counts_for: Mapped[str | None] = mapped_column(Text)
    steps: Mapped[str | None] = mapped_column(Text)
    parse_method: Mapped[str | None] = mapped_column(String(16))


class Option(Base):
    __tablename__ = "options"
    id: Mapped[int] = mapped_column(primary_key=True)
    row_id: Mapped[int] = mapped_column(ForeignKey("rows.id"))
    key: Mapped[str] = mapped_column(String(8))  # 1|2|3|fix
    text: Mapped[str] = mapped_column(Text)


class Impact(Base):
    __tablename__ = "impacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    row_id: Mapped[int] = mapped_column(ForeignKey("rows.id"))
    option_scope: Mapped[str | None] = mapped_column(String(32))
    artifact: Mapped[str] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(Text)
    refs_json: Mapped[str | None] = mapped_column(Text)


class PriorStance(Base):
    __tablename__ = "prior_stances"
    id: Mapped[int] = mapped_column(primary_key=True)
    row_id: Mapped[int] = mapped_column(ForeignKey("rows.id"))
    qa_no: Mapped[int] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(String(64))
    comment: Mapped[str | None] = mapped_column(Text)


class CardLink(Base):
    __tablename__ = "card_links"
    card_id: Mapped[int] = mapped_column(ForeignKey("cards.id"), primary_key=True)
    linked_card_id: Mapped[int] = mapped_column(ForeignKey("cards.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)


class Op(Base):
    __tablename__ = "ops"
    id: Mapped[int] = mapped_column(primary_key=True)
    option_id: Mapped[int] = mapped_column(ForeignKey("options.id"))
    op_json: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(8))
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)


class FailureMap(Base):
    __tablename__ = "failure_map"
    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("versions.id"))
    note_idx: Mapped[int] = mapped_column(Integer)
    crit_id: Mapped[str] = mapped_column(String(32))
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)


class Grade(Base):
    __tablename__ = "grades"
    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("versions.id"))
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id"))
    crit_id: Mapped[str] = mapped_column(String(32))
    pass_no: Mapped[int] = mapped_column(Integer)
    result: Mapped[str | None] = mapped_column(String(32))
    evidence: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[str | None] = mapped_column(String(16))
    final_result: Mapped[str | None] = mapped_column(String(32))
    reviewed_by: Mapped[str | None] = mapped_column(String(64))


class Decision(Base):
    __tablename__ = "decisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("cards.id"))
    source: Mapped[str] = mapped_column(String(16))
    option_key: Mapped[str | None] = mapped_column(String(16))
    scope_json: Mapped[str | None] = mapped_column(Text)
    new_option_text: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[str | None] = mapped_column(String(16))
    flags_json: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[str | None] = mapped_column(String(64))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=utcnow)


class ExpectedChange(Base):
    __tablename__ = "expected_changes"
    id: Mapped[int] = mapped_column(primary_key=True)
    decision_id: Mapped[int] = mapped_column(ForeignKey("decisions.id"))
    artifact: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(Text)
    check_json: Mapped[str | None] = mapped_column(Text)


class Verification(Base):
    __tablename__ = "verifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    expected_change_id: Mapped[int] = mapped_column(ForeignKey("expected_changes.id"))
    new_version_id: Mapped[int] = mapped_column(ForeignKey("versions.id"))
    result: Mapped[str] = mapped_column(String(16))
    evidence: Mapped[str | None] = mapped_column(Text)
    override_by: Mapped[str | None] = mapped_column(String(64))


class UntrackedChange(Base):
    __tablename__ = "untracked_changes"
    id: Mapped[int] = mapped_column(primary_key=True)
    new_version_id: Mapped[int] = mapped_column(ForeignKey("versions.id"))
    artifact: Mapped[str] = mapped_column(String(32))
    diff_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String(16))


class LLMCall(Base):
    __tablename__ = "llm_calls"
    id: Mapped[int] = mapped_column(primary_key=True)
    purpose: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    cost: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    request_hash: Mapped[str | None] = mapped_column(String(64), index=True)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    user: Mapped[str | None] = mapped_column(String(64))
    entity: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    before_json: Mapped[str | None] = mapped_column(Text)
    after_json: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PriorQAItem(Base):
    """Rows of the prior QA sheet (spec 3.3); used to show the full text behind "QA #n"."""

    __tablename__ = "prior_qa_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"))
    source: Mapped[str | None] = mapped_column(String(512))
    qa_no: Mapped[int] = mapped_column(Integer)
    feedback: Mapped[str | None] = mapped_column(Text)
    comment: Mapped[str | None] = mapped_column(Text)
    check_code_location: Mapped[str | None] = mapped_column(Text)
    qa_comment: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String(64))
