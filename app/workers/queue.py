"""Postgres-backed job queue. Workers claim rows with FOR UPDATE SKIP LOCKED, so two workers never take the same job."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.db.models import Job, JobStatus

EVALUATE = "evaluate_course"
PARSE = "parse_transcript"
JOB_TYPES = (PARSE, EVALUATE)
STALE_AFTER = timedelta(minutes=10)  # a RUNNING job this old belongs to a crashed worker


def backoff(attempts: int) -> timedelta:
    """1 min, 2, 4, 8 ... capped at 15 minutes."""
    return timedelta(seconds=min(60 * 2 ** max(attempts - 1, 0), 900))


def claim_job(db: Session, worker_id: str, now: datetime | None = None) -> Job | None:
    now = now or datetime.now(timezone.utc)
    job = db.scalar(
        select(Job)
        .where(
            Job.job_type.in_(JOB_TYPES),
            or_(
                and_(Job.status == JobStatus.QUEUED, Job.next_run_at <= now),
                and_(Job.status == JobStatus.RUNNING, Job.locked_at < now - STALE_AFTER),
            ),
        )
        .order_by(Job.next_run_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        return None
    job.status = JobStatus.RUNNING
    job.attempts += 1
    job.locked_by = worker_id
    job.locked_at = now
    db.commit()
    return job


def mark_done(db: Session, job: Job) -> None:
    job.status = JobStatus.SUCCEEDED
    job.locked_by = None
    job.last_error = None


def requeue(db: Session, job: Job, error: str, now: datetime | None = None) -> None:
    job.status = JobStatus.QUEUED
    job.next_run_at = (now or datetime.now(timezone.utc)) + backoff(job.attempts)
    job.locked_by = None
    job.last_error = error[:500]


def mark_dead(db: Session, job: Job, error: str) -> None:
    """Retries are used up. The case itself has been handed to a person; the job stays for inspection."""
    job.status = JobStatus.DEAD
    job.locked_by = None
    job.last_error = error[:500]
