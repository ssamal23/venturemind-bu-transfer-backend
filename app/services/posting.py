"""Posting decided results to the academic record (grade T).

Deciding and posting are different steps by different people. The credit office posts:
  * courses a department decided (DECIDED), and
  * courses matched to an existing equivalency (MATCHED).

Before anything is posted the rules run once more on live data. A result that was valid when it
was decided can stop being valid (the one-year window closes, another course used up a cap), and
the credit office is the last check before the record changes.

A course is posted once. Posting twice returns the first posting; it never writes a second.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import write_audit
from app.db import models as m
from app.rules.engine import evaluate_course
from app.rules.types import CourseFacts
from app.schemas.posting import PostedRecordOut, PostingCandidateOut
from app.services.rules_service import evaluate_proposed_equivalent, evaluate_stored_course
from app.services.status import change_status

POSTABLE = (m.CourseStatus.DECIDED, m.CourseStatus.MATCHED)
GRADE = "T"


class PostingError(Exception):
    """The course can't be posted. status_code follows HTTP so the API can pass it on."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def result_label(outcome: m.Outcome, bu_code: str | None) -> str:
    if outcome == m.Outcome.BU_EQUIVALENT and bu_code:
        return bu_code
    return "Elective credit" if outcome == m.Outcome.ELECTIVE else "No credit"


def _fmt_credits(value: Decimal | float) -> str:
    return f"{float(value):g}"


# ---------------------------------------------------------------- the queue
def posting_queue(db: Session, limit: int = 200, today: date | None = None) -> list[PostingCandidateOut]:
    """Courses ready to post, oldest first. Blockers shown here are the course-level rules only
    (grade, one-year window), which need no extra queries. Program rules are checked when posting."""
    today = today or date.today()
    rows = db.execute(
        select(m.TranscriptCourse, m.Institution.name, m.User.student_ref, m.TransferApplication.bu_start_date)
        .join(m.Transcript, m.TranscriptCourse.transcript_id == m.Transcript.id)
        .join(m.TransferApplication, m.Transcript.application_id == m.TransferApplication.id)
        .join(m.Institution, m.Transcript.institution_id == m.Institution.id)
        .join(m.User, m.TransferApplication.student_id == m.User.id)
        .where(m.TranscriptCourse.status.in_(POSTABLE), m.TranscriptCourse.final_outcome.is_not(None))
        .order_by(m.TranscriptCourse.updated_at, m.TranscriptCourse.source_code)
        .limit(limit)
    ).all()
    if not rows:
        return []

    ids = [r[0].id for r in rows]
    decisions: dict[uuid.UUID, tuple[m.EvaluationDecision, str]] = {}
    for d, name in db.execute(
        select(m.EvaluationDecision, m.User.name)
        .join(m.User, m.EvaluationDecision.evaluator_id == m.User.id)
        .where(m.EvaluationDecision.transcript_course_id.in_(ids))
        .order_by(m.EvaluationDecision.decided_at)
    ):
        decisions[d.transcript_course_id] = (d, name)  # the latest decision wins
    bu_ids = {r[0].final_bu_course_id for r in rows if r[0].final_bu_course_id}
    bu = {c.id: c.code for c in db.scalars(select(m.Course).where(m.Course.id.in_(bu_ids)))} if bu_ids else {}

    out: list[PostingCandidateOut] = []
    for tc, school, student_ref, start in rows:
        blockers: list[str] = []
        if tc.final_credits and tc.final_credits > 0:  # nothing is awarded for "no credit", so nothing to block
            ev = evaluate_course(CourseFacts(tc.grade, tc.raw_credits, tc.credit_system.value), bu_start_date=start, today=today)
            blockers = [c.label for c in ev.blocking]
        decision = decisions.get(tc.id) if tc.status == m.CourseStatus.DECIDED else None
        out.append(PostingCandidateOut(
            course_id=str(tc.id), student_ref=student_ref or "", course_code=tc.source_code,
            course_title=tc.source_title or "", source_school=school, grade=tc.grade or "",
            result=result_label(tc.final_outcome, bu.get(tc.final_bu_course_id)), outcome=tc.final_outcome.value,
            credits=float(tc.final_credits or 0),
            source="DEPARTMENT_DECISION" if tc.status == m.CourseStatus.DECIDED else "EXISTING_EQUIVALENCY",
            decided_by=decision[1] if decision else None,
            decided_on=f"{decision[0].decided_at:%b} {decision[0].decided_at.day}" if decision else None,
            reason=decision[0].reason if decision else None,
            blockers=blockers,
        ))
    return out


def posting_history(db: Session, limit: int = 50) -> list[PostedRecordOut]:
    rows = db.execute(
        select(m.Posting, m.TranscriptCourse, m.User.student_ref)
        .join(m.TranscriptCourse, m.Posting.transcript_course_id == m.TranscriptCourse.id)
        .join(m.Transcript, m.TranscriptCourse.transcript_id == m.Transcript.id)
        .join(m.TransferApplication, m.Transcript.application_id == m.TransferApplication.id)
        .join(m.User, m.TransferApplication.student_id == m.User.id)
        .order_by(m.Posting.posted_at.desc())
        .limit(limit)
    ).all()
    posters = {u.id: u.name for u in db.scalars(select(m.User).where(m.User.id.in_({r[0].posted_by for r in rows})))} if rows else {}
    codes = {c.id: c.code for c in db.scalars(select(m.Course).where(m.Course.id.in_({r[0].bu_course_id for r in rows if r[0].bu_course_id})))} if rows else {}
    return [
        PostedRecordOut(
            posting_id=str(p.id), course_id=str(tc.id), student_ref=ref or "", course_code=tc.source_code,
            result=result_label(p.outcome, codes.get(p.bu_course_id)), credits_posted=float(p.credits_posted),
            grade_posted=p.grade_posted, posted_at=p.posted_at, posted_by=posters.get(p.posted_by, ""),
        )
        for p, tc, ref in rows
    ]


# ---------------------------------------------------------------- posting
def posting_blockers(db: Session, course: m.TranscriptCourse) -> list[str]:
    """Rules, run again on live data. Empty when nothing is awarded (no credit)."""
    if not course.final_credits or course.final_credits <= 0:
        return []
    checks = list(evaluate_stored_course(db, course).checks)
    if course.final_bu_course_id:
        bu_course = db.get(m.Course, course.final_bu_course_id)
        checks += evaluate_proposed_equivalent(db, course, bu_course).checks
    return [c.label for c in checks if c.blocking]


def post_course(db: Session, course: m.TranscriptCourse, user_id: uuid.UUID, key: str | None = None) -> tuple[m.Posting, bool]:
    """Post one course. Returns (posting, created). The caller commits and holds a lock on the course row."""
    existing = db.scalar(select(m.Posting).where(m.Posting.transcript_course_id == course.id))
    if existing is not None:
        return existing, False  # posting twice is harmless: the first posting stands

    if key and db.scalar(select(m.Posting.id).where(m.Posting.idempotency_key == key)) is not None:
        raise PostingError(409, "That idempotency key was already used for another course.")
    if course.status not in POSTABLE:
        raise PostingError(409, "This course isn't ready to post.")
    if course.final_outcome is None or course.final_credits is None:
        raise PostingError(409, "This course has no final result to post.")
    blockers = posting_blockers(db, course)
    if blockers:
        raise PostingError(409, "Credit can't be posted: " + "; ".join(blockers))

    decision = None
    if course.status == m.CourseStatus.DECIDED:
        decision = db.scalar(
            select(m.EvaluationDecision).where(m.EvaluationDecision.transcript_course_id == course.id)
            .order_by(m.EvaluationDecision.decided_at.desc()).limit(1)
        )
    bu_code = db.get(m.Course, course.final_bu_course_id).code if course.final_bu_course_id else None
    label = result_label(course.final_outcome, bu_code)

    posting = m.Posting(
        transcript_course_id=course.id, decision_id=decision.id if decision else None, posted_by=user_id,
        outcome=course.final_outcome, bu_course_id=course.final_bu_course_id, grade_posted=GRADE,
        credits_posted=course.final_credits, idempotency_key=key or f"post:{course.id}",
        posted_at=datetime.now(timezone.utc),
    )
    db.add(posting)
    db.flush()

    credits = course.final_credits
    message = (f"Posted to your academic record: {label}, {_fmt_credits(credits)} credits, grade {GRADE}"
               if credits > 0 else f"Posted to your academic record: no credit, grade {GRADE}")
    change_status(db, course, m.CourseStatus.POSTED, actor_type=m.ActorType.CREDIT_OFFICE, actor_id=user_id, message=message)
    write_audit(
        db, actor_type=m.ActorType.CREDIT_OFFICE, actor_id=user_id, action="posting.created",
        entity_type="posting", entity_id=posting.id,
        after={"course_id": str(course.id), "result": label, "credits": str(credits), "grade": GRADE,
               "source": "decision" if decision else "existing_equivalency"},
    )
    return posting, True
