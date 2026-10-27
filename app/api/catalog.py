from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import current_user
from app.db.models import Institution, User
from app.db.session import get_session
from app.schemas.transcript import InstitutionOut

router = APIRouter(tags=["catalog"])


@router.get("/institutions", response_model=list[InstitutionOut])
def institutions(_user: User = Depends(current_user), db: Session = Depends(get_session)) -> list[InstitutionOut]:
    """Schools a transcript can come from (everything except BU)."""
    rows = db.scalars(select(Institution).where(Institution.is_bu.is_(False)).order_by(Institution.name))
    return [InstitutionOut(id=str(i.id), name=i.name, calendar_type=i.calendar_type.value) for i in rows]
