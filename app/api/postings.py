import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.db import models as m
from app.db.session import get_session
from app.schemas.posting import (
    BatchItemOut, BatchPostIn, BatchPostOut, PostedRecordOut, PostingCandidateOut, PostingOut,
)
from app.services.posting import PostingError, post_course, posting_history, posting_queue, result_label

router = APIRouter(tags=["credit-office"])
OFFICE = require_role(m.Role.CREDIT_OFFICE)


def _out(db: Session, posting: m.Posting, created: bool) -> PostingOut:
    bu = db.get(m.Course, posting.bu_course_id).code if posting.bu_course_id else None
    return PostingOut(
        posting_id=str(posting.id), course_id=str(posting.transcript_course_id), result=result_label(posting.outcome, bu),
        credits_posted=float(posting.credits_posted), grade_posted=posting.grade_posted, already_posted=not created,
    )


@router.get("/postings/queue", response_model=list[PostingCandidateOut])
def queue(_user: m.User = Depends(OFFICE), db: Session = Depends(get_session)) -> list[PostingCandidateOut]:
    """Decided and matched courses waiting to be posted."""
    return posting_queue(db)


@router.get("/postings", response_model=list[PostedRecordOut])
def history(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    _user: m.User = Depends(OFFICE),
    db: Session = Depends(get_session),
) -> list[PostedRecordOut]:
    """Recently posted results, newest first."""
    return posting_history(db, limit)


@router.post("/transcript-courses/{course_id}/post", response_model=PostingOut)
def post_one(
    course_id: uuid.UUID,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=80)] = None,
    user: m.User = Depends(OFFICE),
    db: Session = Depends(get_session),
) -> PostingOut:
    # Lock the row so two people can't post the same course at the same moment.
    course = db.scalar(select(m.TranscriptCourse).where(m.TranscriptCourse.id == course_id).with_for_update())
    if course is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Course not found.")
    try:
        posting, created = post_course(db, course, user.id, idempotency_key)
    except PostingError as e:
        raise HTTPException(e.status_code, e.detail)
    db.commit()
    return _out(db, posting, created)


@router.post("/postings/batch", response_model=BatchPostOut)
def post_batch(body: BatchPostIn, user: m.User = Depends(OFFICE), db: Session = Depends(get_session)) -> BatchPostOut:
    """Post several courses. Each succeeds or fails on its own; one bad course never blocks the rest."""
    items: list[BatchItemOut] = []
    total = 0.0
    for course_id in dict.fromkeys(body.course_ids):  # drop repeats, keep order
        try:
            with db.begin_nested():  # a failure undoes only this course
                course = db.scalar(select(m.TranscriptCourse).where(m.TranscriptCourse.id == course_id).with_for_update())
                if course is None:
                    raise PostingError(404, "Course not found.")
                posting, created = post_course(db, course, user.id)
        except PostingError as e:
            items.append(BatchItemOut(course_id=str(course_id), result="failed", detail=e.detail))
            continue
        credits = float(posting.credits_posted)
        items.append(BatchItemOut(course_id=str(course_id), result="posted" if created else "already_posted",
                                  credits_posted=credits))
        total += credits if created else 0.0
    db.commit()
    posted = sum(1 for i in items if i.result == "posted")
    return BatchPostOut(items=items, posted_count=posted, failed_count=sum(1 for i in items if i.result == "failed"),
                        credits_posted=round(total, 2))
