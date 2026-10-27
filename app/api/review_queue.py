import uuid
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.db.models import Role, User
from app.db.session import get_session
from app.schemas.evaluation import ReviewCaseOut
from app.services.review_view import build_queue

router = APIRouter(tags=["evaluator"])


@router.get("/review-queue", response_model=list[ReviewCaseOut])
def review_queue(
    _user: User = Depends(require_role(Role.EVALUATOR)),
    db: Session = Depends(get_session),
) -> list[ReviewCaseOut]:
    return build_queue(db)


@router.get("/transcript-courses/{course_id}/workflow")
def workflow(course_id: uuid.UUID, _user: User = Depends(require_role(Role.EVALUATOR))) -> dict[str, Any]:
    """Where this course is in its workflow: waiting_for_evaluator, waiting_for_student, finished, or not_started."""
    from app.workflows.graph import workflow_status

    return workflow_status(course_id)
