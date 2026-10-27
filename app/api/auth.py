from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import create_token, current_user
from app.core.security import hash_password, verify_password
from app.db.models import User
from app.db.session import get_session
from app.schemas.auth import LoginRequest, LoginResponse, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])

# Verified against when the email is unknown, so timing doesn't reveal which emails exist.
_DUMMY_HASH = hash_password("not-a-real-password")


def to_user_out(user: User) -> UserOut:
    return UserOut(
        id=str(user.id),
        name=user.name,
        email=user.email,
        role=user.role.value.lower(),
        department=user.department,
    )


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, db: Session = Depends(get_session)) -> LoginResponse:
    user = db.scalar(select(User).where(User.email == body.email.strip().lower()))
    ok = verify_password(body.password, user.password_hash if user and user.password_hash else _DUMMY_HASH)
    if not user or not user.is_active or not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Email or password is incorrect.")
    return LoginResponse(token=create_token(user), user=to_user_out(user))


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)) -> UserOut:
    return to_user_out(user)
