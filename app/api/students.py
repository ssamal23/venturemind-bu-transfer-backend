import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.db.models import Role, User
from app.db.session import get_session
from app.schemas.transcript import StudentSummaryOut
from app.services.student_view import build_summary

router = APIRouter(prefix="/students", tags=["students"])


@router.get("/{student_id}/dashboard", response_model=StudentSummaryOut)
def dashboard(
    student_id: uuid.UUID,
    user: User = Depends(require_role(Role.STUDENT)),
    db: Session = Depends(get_session),
) -> StudentSummaryOut:
    # A student can only read their own records.
    if student_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You don't have access to this.")
    return build_summary(db, student_id)
