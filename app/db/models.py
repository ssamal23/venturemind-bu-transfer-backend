"""SQLAlchemy 2.x models for the BU transfer credit system (19 tables).

Groups:
  reference   institutions, programs, program_requirements, courses,
              course_matches, past_decisions
  people      users
  intake      transfer_applications, documents, transcripts,
              transcript_courses, syllabi
  evaluation  evaluations, evaluation_decisions, postings
  operations  jobs, llm_calls, status_history, audit_logs

Files live in Azure Blob Storage; `documents` tracks them. LangGraph
checkpoint tables are created by the checkpointer and are not modelled here.
Our tables stay the system of record.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Index, MetaData
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.config import settings

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


# --------------------------------------------------------------------------- enums
class CalendarType(str, enum.Enum):
    SEMESTER = "SEMESTER"
    QUARTER = "QUARTER"


class MatchLevel(str, enum.Enum):
    LIKELY_EQUIVALENT = "LIKELY_EQUIVALENT"
    PARTIAL_SYLLABUS_REVIEW = "PARTIAL_SYLLABUS_REVIEW"
    NO_EQUIVALENT = "NO_EQUIVALENT"
    MUST_TAKE_AT_BU = "MUST_TAKE_AT_BU"


class RequirementType(str, enum.Enum):
    REQUIRED_COURSE = "REQUIRED_COURSE"
    NON_TRANSFERABLE = "NON_TRANSFERABLE"  # for example CS Group C
    MAX_TRANSFER = "MAX_TRANSFER"  # for example max 4 courses at 200+ level
    MUST_TAKE_AT_BU = "MUST_TAKE_AT_BU"  # residency, for example 5 BI courses


class Outcome(str, enum.Enum):
    BU_EQUIVALENT = "BU_EQUIVALENT"
    ELECTIVE = "ELECTIVE"
    NO_CREDIT = "NO_CREDIT"


class DecisionOutcome(str, enum.Enum):
    BU_EQUIVALENT = "BU_EQUIVALENT"
    ELECTIVE = "ELECTIVE"
    NO_CREDIT = "NO_CREDIT"
    REQUEST_INFO = "REQUEST_INFO"


class Role(str, enum.Enum):
    STUDENT = "STUDENT"
    EVALUATOR = "EVALUATOR"
    CREDIT_OFFICE = "CREDIT_OFFICE"


class DocType(str, enum.Enum):
    TRANSCRIPT = "TRANSCRIPT"
    SYLLABUS = "SYLLABUS"


class ScanStatus(str, enum.Enum):
    PENDING = "PENDING"
    CLEAN = "CLEAN"
    REJECTED = "REJECTED"


class ParseStatus(str, enum.Enum):
    PENDING = "PENDING"
    PARSED = "PARSED"
    FAILED = "FAILED"


class ApplicationStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class CourseStatus(str, enum.Enum):
    PARSED = "PARSED"
    MATCHED = "MATCHED"
    AWAITING_SYLLABUS = "AWAITING_SYLLABUS"
    AI_PENDING = "AI_PENDING"
    IN_REVIEW = "IN_REVIEW"
    PENDING_INFO = "PENDING_INFO"
    DECIDED = "DECIDED"
    POSTED = "POSTED"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"


class EvaluationStatus(str, enum.Enum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    NO_SUGGESTION = "NO_SUGGESTION"
    FAILED = "FAILED"


class JobStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DEAD = "DEAD"


class ActorType(str, enum.Enum):
    SYSTEM = "SYSTEM"
    AI = "AI"
    STUDENT = "STUDENT"
    EVALUATOR = "EVALUATOR"
    CREDIT_OFFICE = "CREDIT_OFFICE"


def enum_col(cls: type[enum.Enum], length: int = 32, name: str | None = None) -> sa.Enum:
    """Store enum values as VARCHAR with a CHECK constraint (easy to migrate).

    Pass `name` when two columns in one table share an enum, so the CHECK names differ.
    """
    return sa.Enum(
        cls,
        native_enum=False,
        length=length,
        create_constraint=True,
        values_callable=lambda e: [m.value for m in e],
        name=name or cls.__name__.lower(),
    )


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)


def created() -> Mapped[datetime]:
    return mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def updated() -> Mapped[datetime]:
    return mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
        nullable=False,
    )


def hnsw_index(name: str) -> Index:
    return Index(
        name,
        "embedding",
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


# --------------------------------------------------------------------------- reference data
class Institution(Base):
    """BU plus source schools. calendar_type drives the quarter x 2/3 rule."""

    __tablename__ = "institutions"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(sa.String(200), unique=True)
    short_name: Mapped[str] = mapped_column(sa.String(40))
    calendar_type: Mapped[CalendarType] = mapped_column(enum_col(CalendarType), default=CalendarType.SEMESTER)
    is_bu: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = created()


class Program(Base):
    """One row per major in the workbook. Structured rules live in program_requirements."""

    __tablename__ = "programs"

    id: Mapped[uuid.UUID] = uuid_pk()
    program_area: Mapped[str] = mapped_column(sa.String(80), unique=True)
    bu_program: Mapped[str] = mapped_column(sa.String(200))
    bu_school: Mapped[str] = mapped_column(sa.String(200))
    rules_summary: Mapped[str | None] = mapped_column(sa.Text)  # display text from the workbook
    bu_source_url: Mapped[str | None] = mapped_column(sa.Text)
    program_match: Mapped[str | None] = mapped_column(sa.String(40))
    notes: Mapped[str | None] = mapped_column(sa.Text)
    effective_from: Mapped[date | None] = mapped_column(sa.Date)
    created_at: Mapped[datetime] = created()


class ProgramRequirement(Base):
    """Structured program rules the rules engine can check."""

    __tablename__ = "program_requirements"

    id: Mapped[uuid.UUID] = uuid_pk()
    program_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("programs.id"), index=True)
    requirement_type: Mapped[RequirementType] = mapped_column(enum_col(RequirementType))
    course_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("courses.id"))  # null for group or count rules
    group_label: Mapped[str | None] = mapped_column(sa.String(60))  # for example "Group C"
    max_count: Mapped[int | None]
    min_course_level: Mapped[int | None]
    description: Mapped[str] = mapped_column(sa.Text)
    rule_params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    source_url: Mapped[str | None] = mapped_column(sa.Text)
    effective_from: Mapped[date | None] = mapped_column(sa.Date)


class Course(Base):
    """One catalog for every school (BU and peers) so matching uses one index."""

    __tablename__ = "courses"
    __table_args__ = (
        sa.UniqueConstraint("institution_id", "code", name="uq_courses_institution_code"),
        sa.CheckConstraint("credits >= 0", name="credits_nonneg"),
        hnsw_index("ix_courses_embedding_hnsw"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    institution_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("institutions.id"), index=True)
    program_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("programs.id"), index=True)
    code: Mapped[str] = mapped_column(sa.String(40))
    title: Mapped[str] = mapped_column(sa.String(300))
    requirement_type: Mapped[str | None] = mapped_column(sa.String(80))
    credits: Mapped[Decimal] = mapped_column(sa.Numeric(4, 2))
    description: Mapped[str | None] = mapped_column(sa.Text)
    learning_outcomes: Mapped[list[str]] = mapped_column(JSONB, default=list)
    verified: Mapped[bool] = mapped_column(default=False)
    notes: Mapped[str | None] = mapped_column(sa.Text)
    source_url: Mapped[str | None] = mapped_column(sa.Text)
    embedding: Mapped[Any | None] = mapped_column(Vector(settings.embedding_dim))
    created_at: Mapped[datetime] = created()

    institution: Mapped[Institution] = relationship()


class CourseMatch(Base):
    """The equivalency database: existing source course -> BU course matches."""

    __tablename__ = "course_matches"
    __table_args__ = (sa.UniqueConstraint("source_course_id", "bu_course_id", name="uq_course_matches_pair"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    source_course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("courses.id"), index=True)
    bu_course_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("courses.id"), index=True)
    match_level: Mapped[MatchLevel] = mapped_column(enum_col(MatchLevel))
    notes: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = created()


class PastDecision(Base):
    """Historical decisions (synthetic) used for the accuracy and bias tests."""

    __tablename__ = "past_decisions"

    id: Mapped[uuid.UUID] = uuid_pk()
    source_course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("courses.id"), index=True)
    bu_course_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("courses.id"))
    outcome: Mapped[Outcome] = mapped_column(enum_col(Outcome))
    department: Mapped[str | None] = mapped_column(sa.String(80))
    rationale: Mapped[str | None] = mapped_column(sa.Text)
    decided_on: Mapped[date | None] = mapped_column(sa.Date)


# --------------------------------------------------------------------------- people
class User(Base):
    """Students, department evaluators and credit office staff (synthetic in the demo)."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(sa.String(200), unique=True)
    name: Mapped[str] = mapped_column(sa.String(200))
    role: Mapped[Role] = mapped_column(enum_col(Role), index=True)
    department: Mapped[str | None] = mapped_column(sa.String(80), index=True)  # evaluators
    student_ref: Mapped[str | None] = mapped_column(sa.String(40), unique=True)  # students
    password_hash: Mapped[str | None] = mapped_column(sa.String(200))  # demo only; use SSO in production
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = created()


# --------------------------------------------------------------------------- intake
class TransferApplication(Base):
    """One transfer-credit application per student. Owns the one-year deadline."""

    __tablename__ = "transfer_applications"

    id: Mapped[uuid.UUID] = uuid_pk()
    student_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    program_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("programs.id"))
    status: Mapped[ApplicationStatus] = mapped_column(enum_col(ApplicationStatus), default=ApplicationStatus.DRAFT)
    bu_start_date: Mapped[date | None] = mapped_column(sa.Date)  # starts the 1-year window
    deadline_on: Mapped[date | None] = mapped_column(sa.Date)  # bu_start_date + 1 year
    created_at: Mapped[datetime] = created()
    submitted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    transcripts: Mapped[list[Transcript]] = relationship(back_populates="application")


class Document(Base):
    """Tracks uploaded files. Bytes live in Azure Blob Storage, never in Postgres."""

    __tablename__ = "documents"
    __table_args__ = (
        sa.UniqueConstraint("container", "blob_path", name="uq_documents_blob"),
        sa.CheckConstraint("size_bytes > 0", name="size_positive"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    doc_type: Mapped[DocType] = mapped_column(enum_col(DocType))
    uploaded_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    container: Mapped[str] = mapped_column(sa.String(80))
    blob_path: Mapped[str] = mapped_column(sa.String(400))
    original_filename: Mapped[str] = mapped_column(sa.String(300))
    content_type: Mapped[str] = mapped_column(sa.String(100))
    size_bytes: Mapped[int] = mapped_column(sa.BigInteger)
    sha256: Mapped[str] = mapped_column(sa.String(64), index=True)
    scan_status: Mapped[ScanStatus] = mapped_column(enum_col(ScanStatus), default=ScanStatus.PENDING)
    uploaded_at: Mapped[datetime] = created()
    deleted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class Transcript(Base):
    __tablename__ = "transcripts"

    id: Mapped[uuid.UUID] = uuid_pk()
    application_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transfer_applications.id"), index=True)
    institution_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("institutions.id"))
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), unique=True)
    parse_status: Mapped[ParseStatus] = mapped_column(enum_col(ParseStatus), default=ParseStatus.PENDING)
    parse_error: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = created()
    parsed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    application: Mapped[TransferApplication] = relationship(back_populates="transcripts")
    courses: Mapped[list[TranscriptCourse]] = relationship(back_populates="transcript", cascade="all, delete-orphan")


class TranscriptCourse(Base):
    """One course line on a transcript. This row carries the live status the student sees."""

    __tablename__ = "transcript_courses"
    __table_args__ = (
        sa.UniqueConstraint("transcript_id", "source_code", "term", name="uq_transcript_courses_line"),
        sa.CheckConstraint("raw_credits >= 0", name="raw_credits_nonneg"),
        Index("ix_transcript_courses_status", "status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    transcript_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transcripts.id"), index=True)
    source_course_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("courses.id"))  # set when catalogued
    course_match_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("course_matches.id"))
    source_code: Mapped[str] = mapped_column(sa.String(40))
    source_title: Mapped[str | None] = mapped_column(sa.String(300))
    term: Mapped[str | None] = mapped_column(sa.String(40))
    grade: Mapped[str | None] = mapped_column(sa.String(8))
    raw_credits: Mapped[Decimal] = mapped_column(sa.Numeric(4, 2))
    credit_system: Mapped[CalendarType] = mapped_column(enum_col(CalendarType), default=CalendarType.SEMESTER)
    converted_credits: Mapped[Decimal | None] = mapped_column(sa.Numeric(4, 2))  # quarter x 2/3 applied
    status: Mapped[CourseStatus] = mapped_column(enum_col(CourseStatus), default=CourseStatus.PARSED)
    rule_results: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    rules_version: Mapped[str | None] = mapped_column(sa.String(20))
    status_note: Mapped[str | None] = mapped_column(sa.Text)
    final_outcome: Mapped[Outcome | None] = mapped_column(enum_col(Outcome))
    final_bu_course_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("courses.id"))
    final_credits: Mapped[Decimal | None] = mapped_column(sa.Numeric(4, 2))
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = updated()

    transcript: Mapped[Transcript] = relationship(back_populates="courses")
    history: Mapped[list[StatusHistory]] = relationship(back_populates="course", order_by="StatusHistory.created_at")


class Syllabus(Base):
    __tablename__ = "syllabi"
    __table_args__ = (
        sa.UniqueConstraint("transcript_course_id", "version", name="uq_syllabi_version"),
        hnsw_index("ix_syllabi_embedding_hnsw"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    transcript_course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transcript_courses.id"), index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), unique=True)
    version: Mapped[int] = mapped_column(default=1)
    extracted_text: Mapped[str | None] = mapped_column(sa.Text)  # untrusted data, never instructions
    learning_outcomes: Mapped[list[str]] = mapped_column(JSONB, default=list)
    embedding: Mapped[Any | None] = mapped_column(Vector(settings.embedding_dim))
    created_at: Mapped[datetime] = created()


# --------------------------------------------------------------------------- evaluation
class Evaluation(Base):
    """One AI run: candidates, suggestion, evidence, and which model produced it."""

    __tablename__ = "evaluations"
    __table_args__ = (
        sa.CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="confidence_range"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    transcript_course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transcript_courses.id"), index=True)
    syllabus_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("syllabi.id"))
    status: Mapped[EvaluationStatus] = mapped_column(enum_col(EvaluationStatus), default=EvaluationStatus.PENDING)
    candidates: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)  # [{course_id, score}]
    suggested_outcome: Mapped[Outcome | None] = mapped_column(enum_col(Outcome))
    suggested_bu_course_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("courses.id"))
    confidence: Mapped[Decimal | None] = mapped_column(sa.Numeric(4, 3))
    evidence: Mapped[list[str]] = mapped_column(JSONB, default=list)
    gaps: Mapped[str | None] = mapped_column(sa.Text)
    provider: Mapped[str | None] = mapped_column(sa.String(40))  # claude | gemini | ...
    model: Mapped[str | None] = mapped_column(sa.String(80))
    prompt_version: Mapped[str | None] = mapped_column(sa.String(20))
    validation_errors: Mapped[list[str]] = mapped_column(JSONB, default=list)
    langgraph_thread_id: Mapped[str | None] = mapped_column(sa.String(80), index=True)
    created_at: Mapped[datetime] = created()


class EvaluationDecision(Base):
    """Human decision. Rows are never edited; a new decision supersedes the old one."""

    __tablename__ = "evaluation_decisions"
    __table_args__ = (sa.CheckConstraint("length(trim(reason)) > 0", name="reason_required"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    transcript_course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transcript_courses.id"), index=True)
    evaluation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("evaluations.id"))
    evaluator_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    outcome: Mapped[DecisionOutcome] = mapped_column(enum_col(DecisionOutcome))
    bu_course_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("courses.id"))
    credits_awarded: Mapped[Decimal | None] = mapped_column(sa.Numeric(4, 2))
    reason: Mapped[str] = mapped_column(sa.Text)
    overrode_ai: Mapped[bool] = mapped_column(default=False)
    decided_at: Mapped[datetime] = created()


class Posting(Base):
    """What was posted to the academic record (grade T). Separate from the decision, and never edited.

    A course is posted once. The row is a snapshot of the result at the time, so it still reads
    correctly if anything upstream is later corrected.
    """

    __tablename__ = "postings"

    id: Mapped[uuid.UUID] = uuid_pk()
    transcript_course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transcript_courses.id"), unique=True)
    # Set when a department decided the course; empty when an existing equivalency was applied.
    decision_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("evaluation_decisions.id"), unique=True)
    posted_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    outcome: Mapped[Outcome] = mapped_column(enum_col(Outcome))
    bu_course_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("courses.id"))
    grade_posted: Mapped[str] = mapped_column(sa.String(4), default="T")
    credits_posted: Mapped[Decimal] = mapped_column(sa.Numeric(4, 2))
    idempotency_key: Mapped[str] = mapped_column(sa.String(80), unique=True)
    posted_at: Mapped[datetime] = created()


# --------------------------------------------------------------------------- operations
class Job(Base):
    """Durable job queue (Postgres-backed; swap for Service Bus later)."""

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_pickup", "status", "next_run_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    job_type: Mapped[str] = mapped_column(sa.String(40))
    transcript_course_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("transcript_courses.id"), index=True)
    transcript_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("transcripts.id"), index=True)  # parse_transcript jobs
    status: Mapped[JobStatus] = mapped_column(enum_col(JobStatus), default=JobStatus.QUEUED)
    attempts: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=5)
    next_run_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now())
    idempotency_key: Mapped[str] = mapped_column(sa.String(120), unique=True)
    locked_by: Mapped[str | None] = mapped_column(sa.String(80))
    locked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = updated()


class LlmCall(Base):
    """Every model call, for cost, latency and fallback reporting (no raw student text)."""

    __tablename__ = "llm_calls"

    id: Mapped[uuid.UUID] = uuid_pk()
    evaluation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("evaluations.id"), index=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id"))
    purpose: Mapped[str] = mapped_column(sa.String(40))  # parse_transcript | match_reasoning
    provider: Mapped[str] = mapped_column(sa.String(40), index=True)
    model: Mapped[str] = mapped_column(sa.String(80))
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    latency_ms: Mapped[int | None]
    success: Mapped[bool]
    error_type: Mapped[str | None] = mapped_column(sa.String(60))  # rate_limit | timeout | overloaded | invalid_output
    fallback_used: Mapped[bool] = mapped_column(default=False)
    trace_id: Mapped[str | None] = mapped_column(sa.String(80))  # Langfuse trace
    created_at: Mapped[datetime] = created()


class StatusHistory(Base):
    """Feeds the student timeline. One row per status change."""

    __tablename__ = "status_history"

    id: Mapped[uuid.UUID] = uuid_pk()
    transcript_course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transcript_courses.id"), index=True)
    from_status: Mapped[CourseStatus | None] = mapped_column(enum_col(CourseStatus, name="from_status"))
    to_status: Mapped[CourseStatus] = mapped_column(enum_col(CourseStatus, name="to_status"))
    actor_type: Mapped[ActorType] = mapped_column(enum_col(ActorType))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    message: Mapped[str] = mapped_column(sa.String(300))
    created_at: Mapped[datetime] = created()

    course: Mapped[TranscriptCourse] = relationship(back_populates="history")


class AuditLog(Base):
    """Append-only. A trigger (sql/audit_append_only.sql) blocks UPDATE and DELETE."""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_entity", "entity_type", "entity_id"),)

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    actor_type: Mapped[ActorType] = mapped_column(enum_col(ActorType))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    action: Mapped[str] = mapped_column(sa.String(60))
    entity_type: Mapped[str] = mapped_column(sa.String(60))
    entity_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    request_id: Mapped[str | None] = mapped_column(sa.String(80))
    created_at: Mapped[datetime] = created()
