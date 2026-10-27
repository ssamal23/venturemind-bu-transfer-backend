from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.common import CamelModel


class PostingCandidateOut(CamelModel):
    """A course whose result is ready to post (or would be, if its blockers were cleared)."""

    course_id: str
    student_ref: str
    course_code: str
    course_title: str
    source_school: str
    grade: str
    result: str  # a BU course code, "Elective credit" or "No credit"
    outcome: str  # BU_EQUIVALENT | ELECTIVE | NO_CREDIT
    credits: float
    source: str  # DEPARTMENT_DECISION | EXISTING_EQUIVALENCY
    decided_by: str | None = None
    decided_on: str | None = None
    reason: str | None = None
    blockers: list[str] = []


class PostingOut(CamelModel):
    posting_id: str
    course_id: str
    result: str
    credits_posted: float
    grade_posted: str
    already_posted: bool = False


class PostedRecordOut(CamelModel):
    posting_id: str
    course_id: str
    student_ref: str
    course_code: str
    result: str
    credits_posted: float
    grade_posted: str
    posted_at: datetime
    posted_by: str


class BatchPostIn(CamelModel):
    course_ids: list[UUID] = Field(min_length=1, max_length=100)


class BatchItemOut(CamelModel):
    course_id: str
    result: str  # posted | already_posted | failed
    detail: str | None = None
    credits_posted: float | None = None


class BatchPostOut(CamelModel):
    items: list[BatchItemOut]
    posted_count: int
    failed_count: int
    credits_posted: float
