"""Builds the student dashboard from the database."""

import uuid
from collections import defaultdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Course,
    CourseStatus,
    Evaluation,
    Institution,
    Outcome,
    ParseStatus,
    StatusHistory,
    Transcript,
    TranscriptCourse,
    TransferApplication,
)
from app.schemas.transcript import StudentSummaryOut, TimelineEventOut, TranscriptNoticeOut, TransferCourseOut
from app.services.departments import department_for

# The database has nine statuses; the student sees five.
DISPLAY_STATUS = {
    CourseStatus.PARSED: "IN_REVIEW",
    CourseStatus.MATCHED: "MATCHED",
    CourseStatus.AWAITING_SYLLABUS: "AWAITING_SYLLABUS",
    CourseStatus.AI_PENDING: "IN_REVIEW",
    CourseStatus.IN_REVIEW: "IN_REVIEW",
    CourseStatus.PENDING_INFO: "AWAITING_SYLLABUS",
    CourseStatus.DECIDED: "DECIDED",
    CourseStatus.POSTED: "POSTED",
    CourseStatus.NOT_ELIGIBLE: "NOT_ELIGIBLE",
}

DEFAULT_NOTE = {
    CourseStatus.PARSED: "Being processed.",
    CourseStatus.AWAITING_SYLLABUS: "Upload the course syllabus to continue.",
    CourseStatus.AI_PENDING: "Syllabus received. Evaluation has started.",
    CourseStatus.POSTED: "Posted to your academic record.",
}


def format_date(dt) -> str:
    return f"{dt:%b} {dt.day}"


def build_summary(db: Session, student_id: uuid.UUID, today: date | None = None) -> StudentSummaryOut:
    today = today or date.today()
    rows = db.execute(
        select(TranscriptCourse, Institution.name, TransferApplication.deadline_on)
        .join(Transcript, TranscriptCourse.transcript_id == Transcript.id)
        .join(TransferApplication, Transcript.application_id == TransferApplication.id)
        .join(Institution, Transcript.institution_id == Institution.id)
        .where(TransferApplication.student_id == student_id)
        .order_by(TranscriptCourse.created_at, TranscriptCourse.source_code)
    ).all()

    course_ids = [r[0].id for r in rows]
    history: dict[uuid.UUID, list[StatusHistory]] = defaultdict(list)
    if course_ids:
        for h in db.scalars(
            select(StatusHistory)
            .where(StatusHistory.transcript_course_id.in_(course_ids))
            .order_by(StatusHistory.created_at)
        ):
            history[h.transcript_course_id].append(h)

    # Latest AI suggestion per course, used only to name the reviewing department.
    suggested: dict[uuid.UUID, uuid.UUID] = {}
    review_ids = [r[0].id for r in rows if r[0].status == CourseStatus.IN_REVIEW]
    if review_ids:
        for ev in db.scalars(
            select(Evaluation)
            .where(Evaluation.transcript_course_id.in_(review_ids), Evaluation.suggested_bu_course_id.is_not(None))
            .order_by(Evaluation.created_at)
        ):
            suggested[ev.transcript_course_id] = ev.suggested_bu_course_id

    bu_ids = {r[0].final_bu_course_id for r in rows if r[0].final_bu_course_id} | set(suggested.values())
    bu_courses = {c.id: c for c in db.scalars(select(Course).where(Course.id.in_(bu_ids)))} if bu_ids else {}

    courses: list[TransferCourseOut] = []
    for tc, school, _deadline in rows:
        if tc.final_outcome == Outcome.BU_EQUIVALENT and tc.final_bu_course_id:
            bu_result: str | None = bu_courses[tc.final_bu_course_id].code
        elif tc.final_outcome == Outcome.ELECTIVE:
            bu_result = "Elective credit"
        elif tc.final_outcome == Outcome.NO_CREDIT:
            bu_result = "No credit"
        else:
            bu_result = None

        note = tc.status_note or DEFAULT_NOTE.get(tc.status)
        if tc.status == CourseStatus.IN_REVIEW and not tc.status_note:
            suggestion = bu_courses.get(suggested.get(tc.id))
            code = suggestion.code if suggestion else None
            note = f"With the {department_for(code, tc.source_code)} department."

        courses.append(
            TransferCourseOut(
                id=str(tc.id),
                source_code=tc.source_code,
                source_school=school,
                grade=tc.grade or "",
                bu_result=bu_result,
                credits=float(tc.final_credits) if tc.final_credits is not None else None,
                status=DISPLAY_STATUS[tc.status],
                note=note,
                timeline=[
                    TimelineEventOut(date=format_date(h.created_at), label=h.message) for h in history[tc.id]
                ],
            )
        )

    deadlines = [r[2] for r in rows if r[2]]
    days_left = (min(deadlines) - today).days if deadlines else None
    notices = [
        TranscriptNoticeOut(id=str(t.id), school=school, status=t.parse_status.value, error=t.parse_error)
        for t, school in db.execute(
            select(Transcript, Institution.name)
            .join(TransferApplication, Transcript.application_id == TransferApplication.id)
            .join(Institution, Transcript.institution_id == Institution.id)
            .where(TransferApplication.student_id == student_id, Transcript.parse_status != ParseStatus.PARSED)
            .order_by(Transcript.created_at)
        )
    ]
    return StudentSummaryOut(days_left=days_left, courses=courses, transcripts=notices)
