from typing import Literal

from pydantic import Field

from app.schemas.common import CamelModel


class DecisionIn(CamelModel):
    outcome: Literal["BU_EQUIVALENT", "ELECTIVE", "NO_CREDIT", "REQUEST_INFO"]
    reason: str = Field(min_length=1, max_length=2000)
    bu_course_code: str | None = Field(default=None, max_length=40)


class DecisionOut(CamelModel):
    course_id: str
    course_status: str
    credits_awarded: float | None
