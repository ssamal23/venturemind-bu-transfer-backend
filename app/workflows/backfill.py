"""Save a workflow for courses that are already waiting but have none.

    python -m app.workflows.backfill

Courses created before the workflow existed (or restored from a backup) are decided
directly, with the same checks. This gives them a saved pause so the workflow is the single
path for every waiting course. Safe to run again.
"""

from sqlalchemy import select

from app.db.models import CourseStatus, TranscriptCourse
from app.db.session import SessionLocal
from app.workflows.checkpointer import close
from app.workflows.graph import park_at_review, park_at_syllabus


def main() -> None:
    parked = {"in_review": 0, "pending_info": 0}
    try:
        with SessionLocal() as db:
            for course in db.scalars(select(TranscriptCourse).where(
                    TranscriptCourse.status.in_([CourseStatus.IN_REVIEW, CourseStatus.PENDING_INFO]))):
                if course.status == CourseStatus.IN_REVIEW and park_at_review(course.id):
                    parked["in_review"] += 1
                elif course.status == CourseStatus.PENDING_INFO and park_at_syllabus(course.id):
                    parked["pending_info"] += 1
    finally:
        close()
    print(f"Saved a workflow for {parked['in_review']} course(s) in review and {parked['pending_info']} waiting for a syllabus.")


if __name__ == "__main__":
    main()
