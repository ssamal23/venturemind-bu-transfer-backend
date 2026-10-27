from pydantic import BaseModel, Field

from app.schemas.common import CamelModel


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class UserOut(CamelModel):
    id: str
    name: str
    email: str
    role: str  # "student" | "evaluator" | "credit_office"
    department: str | None = None


class LoginResponse(CamelModel):
    token: str
    user: UserOut
