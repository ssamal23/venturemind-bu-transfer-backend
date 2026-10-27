from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.session import get_session

router = APIRouter(tags=["health"])


@router.get("/health")
def health(db: Session = Depends(get_session)) -> dict[str, str]:
    try:
        db.execute(text("select 1"))
    except Exception:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Database unavailable.")
    return {"status": "ok"}
