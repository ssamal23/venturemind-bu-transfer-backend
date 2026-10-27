"""Builds the evaluator review queue from the database."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Course,
    CourseStatus,
    Evaluation,
    EvaluationStatus,
    Institution,
    StatusHistory,
    Syllabus,
    Transcript,
    TranscriptCourse,
    TransferApplication,
    User,
)
from app.schemas.evaluation import AiSuggestionOut, BuCourseOut, ReviewCaseOut, RuleCheckOut
from app.services.departments import department_for

PRIMARY_PROVIDER = "claude"
MODEL_NAMES = {
    "claude-sonnet-5-5": "Claude Sonnet 5.5",
    "claude-haiku-5-5": "Claude Haiku 5.5",
    "gemini-2.5-flash": "Gemini Flash",
}


def model_label(ev: Evaluation) -> str:
    name = MODEL_NAMES.get(ev.model or "", ev.model or ev.provider or "Unknown model")
    return name if ev.provider == PRIMARY_PROVIDER else f"{name} (fallback)"


def build_queue(db: Session) -> list[ReviewCaseOut]:
    rows = db.execute(
        select(TranscriptCourse, Institution.name, User.student_ref)
        .join(Transcript, TranscriptCourse.transcript_id == Transcript.id)
        .join(TransferApplication, Transcript.application_id == TransferApplication.id)
        .join(Institution, Transcript.institution_id == Institution.id)
        .join(User, TransferApplication.student_id == User.id)
        .where(TranscriptCourse.status == CourseStatus.IN_REVIEW)
    ).all()
    if not rows:
        return []

    ids = [r[0].id for r in rows]
    latest_eval: dict[uuid.UUID, Evaluation] = {}
    for ev in db.scalars(
        select(Evaluation).where(Evaluation.transcript_course_id.in_(ids)).order_by(Evaluation.created_at)
    ):
        latest_eval[ev.transcript_course_id] = ev

    latest_syllabus: dict[uuid.UUID, Syllabus] = {}
    for sy in db.scalars(select(Syllabus).where(Syllabus.transcript_course_id.in_(ids)).order_by(Syllabus.version)):
        latest_syllabus[sy.transcript_course_id] = sy

    review_since: dict[uuid.UUID, datetime] = {}
    for h in db.scalars(
        select(StatusHistory)
        .where(StatusHistory.transcript_course_id.in_(ids), StatusHistory.to_status == CourseStatus.IN_REVIEW)
        .order_by(StatusHistory.created_at)
    ):
        review_since[h.transcript_course_id] = h.created_at

    bu_ids = {e.suggested_bu_course_id for e in latest_eval.values() if e.suggested_bu_course_id}
    bu_courses = {c.id: c for c in db.scalars(select(Course).where(Course.id.in_(bu_ids)))} if bu_ids else {}

    now = datetime.now(timezone.utc)
    cases: list[ReviewCaseOut] = []
    for tc, school, student_ref in rows:
        ev = latest_eval.get(tc.id)
        sy = latest_syllabus.get(tc.id)
        bu = bu_courses.get(ev.suggested_bu_course_id) if ev and ev.suggested_bu_course_id else None

        ai = None
        if ev and ev.status == EvaluationStatus.COMPLETED and ev.suggested_outcome and ev.confidence is not None:
            ai = AiSuggestionOut(
                outcome=ev.suggested_outcome.value,
                bu_course=bu.code if bu else None,
                confidence=float(ev.confidence),
                evidence=list(ev.evidence or []),
                gaps=ev.gaps or "",
                model=model_label(ev),
            )

        since = review_since.get(tc.id)
        cases.append(
            ReviewCaseOut(
                id=str(tc.id),
                student_ref=student_ref or "",
                course_code=tc.source_code,
                course_title=tc.source_title or "",
                source_school=school,
                department=department_for(bu.code if bu else None, tc.source_code),
                credits=float(tc.raw_credits),
                grade=tc.grade or "",
                waiting_days=max((now - since).days, 0) if since else 0,
                syllabus_outcomes=list(sy.learning_outcomes or []) if sy else [],
                syllabus_notes=(sy.extracted_text or "") if sy else "",
                bu_course=(
                    BuCourseOut(
                        code=bu.code,
                        title=bu.title,
                        credits=float(bu.credits),
                        outcomes=list(bu.learning_outcomes or []),
                    )
                    if bu
                    else None
                ),
                ai=ai,
                rules=[
                    RuleCheckOut(label=r["label"], pass_=bool(r.get("pass")), note=bool(r.get("note")))
                    for r in (tc.rule_results or [])
                ],
            )
        )
    cases.sort(key=lambda c: -c.waiting_days)
    return cases
