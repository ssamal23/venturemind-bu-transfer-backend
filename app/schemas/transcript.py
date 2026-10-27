from app.schemas.common import CamelModel


class TimelineEventOut(CamelModel):
    date: str
    label: str


class TransferCourseOut(CamelModel):
    id: str
    source_code: str
    source_school: str
    grade: str
    bu_result: str | None
    credits: float | None
    status: str  # MATCHED | DECIDED | AWAITING_SYLLABUS | IN_REVIEW | NOT_ELIGIBLE
    note: str | None = None
    timeline: list[TimelineEventOut]


class TranscriptNoticeOut(CamelModel):
    """A transcript that is still being read, or that could not be."""

    id: str
    school: str
    status: str  # PENDING | FAILED
    error: str | None = None


class StudentSummaryOut(CamelModel):
    days_left: int | None
    courses: list[TransferCourseOut]
    transcripts: list[TranscriptNoticeOut] = []


class SyllabusUploadOut(CamelModel):
    course_id: str
    status: str
    version: int


class InstitutionOut(CamelModel):
    id: str
    name: str
    calendar_type: str  # SEMESTER | QUARTER


class TranscriptUploadOut(CamelModel):
    transcript_id: str
    status: str


class TranscriptStatusOut(CamelModel):
    id: str
    school: str
    status: str  # PENDING | PARSED | FAILED
    error: str | None = None
    course_count: int
