from pydantic import Field

from app.schemas.common import CamelModel


class RuleCheckOut(CamelModel):
    label: str
    pass_: bool = Field(alias="pass")  # "pass" is a Python keyword
    note: bool = False


class BuCourseOut(CamelModel):
    code: str
    title: str
    credits: float
    outcomes: list[str]


class AiSuggestionOut(CamelModel):
    outcome: str
    bu_course: str | None
    confidence: float
    evidence: list[str]
    gaps: str
    model: str


class ReviewCaseOut(CamelModel):
    id: str
    student_ref: str
    course_code: str
    course_title: str
    source_school: str
    department: str
    credits: float
    grade: str
    waiting_days: int
    syllabus_outcomes: list[str]
    syllabus_notes: str
    bu_course: BuCourseOut | None
    ai: AiSuggestionOut | None
    rules: list[RuleCheckOut]
