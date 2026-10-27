"""Course status changes. The database does not enforce legal transitions; this does."""

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.audit import write_audit
from app.db.models import ActorType, CourseStatus, StatusHistory, TranscriptCourse

ALLOWED: dict[CourseStatus, set[CourseStatus]] = {
    CourseStatus.PARSED: {CourseStatus.MATCHED, CourseStatus.NOT_ELIGIBLE, CourseStatus.AWAITING_SYLLABUS},
    CourseStatus.AWAITING_SYLLABUS: {CourseStatus.AI_PENDING},
    CourseStatus.AI_PENDING: {CourseStatus.IN_REVIEW},
    CourseStatus.IN_REVIEW: {CourseStatus.PENDING_INFO, CourseStatus.DECIDED},
    CourseStatus.PENDING_INFO: {CourseStatus.AI_PENDING},
    CourseStatus.DECIDED: {CourseStatus.POSTED},
    CourseStatus.MATCHED: {CourseStatus.POSTED},
    CourseStatus.NOT_ELIGIBLE: set(),
    CourseStatus.POSTED: set(),
}


def change_status(
    db: Session,
    course: TranscriptCourse,
    to: CourseStatus,
    *,
    actor_type: ActorType,
    actor_id: uuid.UUID | None,
    message: str,
    note: str | None = None,
) -> None:
    """Move a course to a new status, write the timeline row and the audit row."""
    if to not in ALLOWED[course.status]:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"A course in {course.status.value} can't move to {to.value}."
        )
    previous = course.status
    course.status = to
    course.status_note = note
    db.add(
        StatusHistory(
            transcript_course_id=course.id,
            from_status=previous,
            to_status=to,
            actor_type=actor_type,
            actor_id=actor_id,
            message=message[:300],
            created_at=datetime.now(timezone.utc),  # Python-side, so rows written in one transaction still order
        )
    )
    write_audit(
        db,
        actor_type=actor_type,
        actor_id=actor_id,
        action="course.status_changed",
        entity_type="transcript_course",
        entity_id=course.id,
        before={"status": previous.value},
        after={"status": to.value},
    )
