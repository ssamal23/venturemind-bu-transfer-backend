"""Glue between the pure rules engine and the database."""

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Course,
    CourseMatch,
    CourseStatus,
    MatchLevel,
    ProgramRequirement,
    Transcript,
    TranscriptCourse,
    TransferApplication,
)
from app.rules.engine import evaluate_course, evaluate_target
from app.rules.types import CourseFacts, Evaluation, ProgramRule

CREDITED = (CourseStatus.MATCHED, CourseStatus.DECIDED, CourseStatus.POSTED)


def application_for(db: Session, course: TranscriptCourse) -> TransferApplication:
    return db.execute(
        select(TransferApplication)
        .join(Transcript, Transcript.application_id == TransferApplication.id)
        .where(Transcript.id == course.transcript_id)
    ).scalar_one()


def evaluate_stored_course(db: Session, course: TranscriptCourse, today: date | None = None) -> Evaluation:
    """Run the course-level rules on a transcript line using the application's start date."""
    app = application_for(db, course)
    facts = CourseFacts(grade=course.grade, raw_credits=course.raw_credits, credit_system=course.credit_system.value)
    return evaluate_course(facts, bu_start_date=app.bu_start_date, today=today or date.today())


def program_rules_for(db: Session, bu_course: Course) -> list[ProgramRule]:
    if bu_course.program_id is None:
        return []
    rows = db.scalars(select(ProgramRequirement).where(ProgramRequirement.program_id == bu_course.program_id)).all()
    ids = {r.course_id for r in rows if r.course_id}
    codes = {c.id: c.code for c in db.scalars(select(Course).where(Course.id.in_(ids)))} if ids else {}
    return [
        ProgramRule(
            type=r.requirement_type.value,
            course_code=codes.get(r.course_id),
            group_label=r.group_label,
            max_count=r.max_count,
            min_level=r.min_course_level,
            description=r.description,
            params=r.rule_params or {},
        )
        for r in rows
    ]


def credited_bu_codes(db: Session, student_id: uuid.UUID, exclude_course_id: uuid.UUID) -> list[str]:
    """BU courses this student has already been credited with (for count limits)."""
    return list(
        db.scalars(
            select(Course.code)
            .join(TranscriptCourse, TranscriptCourse.final_bu_course_id == Course.id)
            .join(Transcript, TranscriptCourse.transcript_id == Transcript.id)
            .join(TransferApplication, Transcript.application_id == TransferApplication.id)
            .where(
                TransferApplication.student_id == student_id,
                TranscriptCourse.id != exclude_course_id,
                TranscriptCourse.status.in_(CREDITED),
            )
        )
    )


def evaluate_proposed_equivalent(db: Session, course: TranscriptCourse, bu_course: Course) -> Evaluation:
    """Check a proposed BU equivalent against its program's rules and the equivalency database."""
    app = application_for(db, course)
    must_take = (
        db.scalar(
            select(CourseMatch.id).where(
                CourseMatch.bu_course_id == bu_course.id, CourseMatch.match_level == MatchLevel.MUST_TAKE_AT_BU
            )
        )
        is not None
    )
    return evaluate_target(
        bu_course.code,
        program_rules_for(db, bu_course),
        credited_bu_codes(db, app.student_id, course.id),
        must_take_at_bu=must_take,
    )
