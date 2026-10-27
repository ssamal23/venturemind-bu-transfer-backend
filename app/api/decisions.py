import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.db.models import Role, TranscriptCourse, User
from app.db.session import get_session
from app.schemas.decision import DecisionIn, DecisionOut
from app.services.decisions import DecisionError, DecisionRequest, plan_decision
from app.workflows.graph import submit_decision

router = APIRouter(tags=["evaluator"])


@router.post("/transcript-courses/{course_id}/decision", response_model=DecisionOut)
def record_decision(
    course_id: uuid.UUID,
    body: DecisionIn,
    user: User = Depends(require_role(Role.EVALUATOR)),
    db: Session = Depends(get_session),
) -> DecisionOut:
    # Lock the row so two evaluators can't decide the same course at once.
    course = db.scalar(select(TranscriptCourse).where(TranscriptCourse.id == course_id).with_for_update())
    if course is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Course not found.")

    req = DecisionRequest(evaluator_id=user.id, outcome=body.outcome, reason=body.reason, bu_course_code=body.bu_course_code)
    try:
        plan_decision(db, course, req)  # refuse a bad request before the paused workflow is touched
        result = submit_decision(db, course, req)  # resumes the workflow; its record step checks again and writes
    except DecisionError as e:
        raise HTTPException(e.status_code, e.detail)
    db.commit()
    return DecisionOut(course_id=result.course_id, course_status=result.course_status, credits_awarded=result.credits_awarded)
