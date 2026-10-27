"""Background worker. Two kinds of job:
  evaluate_course   turns an uploaded syllabus into an AI suggestion for the evaluator (below)
  parse_transcript  reads an uploaded transcript into course rows (see transcript_worker.py)

Turns an uploaded syllabus into an AI suggestion for the evaluator.

    python -m app.workers.evaluation_worker            # keep polling
    python -m app.workers.evaluation_worker --once     # process what is queued, then stop

Per job: safety check the file, read the text, remove personal details, find the closest
BU courses, ask the LLM gateway, check the answer in code, save it, and move the course
to IN_REVIEW. The worker only ever writes a SUGGESTION. It never decides anything.

If no AI provider is available the job is retried later. After the last retry the case
still goes to a human, marked "no AI suggestion", so a student is never left waiting on
an outage and nothing is approved automatically.
"""

from __future__ import annotations

import argparse
import logging
import socket
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import PurePath

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.core.audit import write_audit
from app.core.redaction import detect_injection, redact
from app.core.storage import get_store
from app.db.models import (
    ActorType, Course, CourseStatus, Document, Evaluation, EvaluationStatus, Job, LlmCall,
    Outcome, ScanStatus, Syllabus, TranscriptCourse, Institution, Transcript,
)
from app.db.session import SessionLocal
from app.llm.gateway import AllProvidersFailed, CallRecord, LLMGateway, build_gateway
from app.llm.matching import PROMPT_VERSION, CandidateInfo, build_request, make_validator
from app.services.departments import department_for
from app.services.doc_scan import is_safe
from app.services.embeddings import Embedder, get_embedder
from app.services.retrieval import retrieve_by_vector
from app.services.status import change_status
from app.services.text_extract import ExtractError, extract_text
from app.workers.queue import PARSE, claim_job, mark_dead, mark_done, requeue
from app.workflows.graph import run_evaluation

log = logging.getLogger("worker")
GatewayFactory = Callable[..., LLMGateway]
INJECTION_CONFIDENCE_CAP = 0.5


def _save_calls(db: Session, calls: list[CallRecord], job: Job, evaluation: Evaluation | None) -> None:
    for c in calls:
        db.add(LlmCall(
            evaluation_id=evaluation.id if evaluation else None, job_id=job.id, purpose=c.purpose,
            provider=c.provider, model=c.model, input_tokens=c.input_tokens, output_tokens=c.output_tokens,
            latency_ms=c.latency_ms, success=c.success, error_type=c.error_type, fallback_used=c.fallback_used,
        ))


def _hand_to_human(db: Session, job: Job, course: TranscriptCourse, syllabus: Syllabus | None, reason: str,
                   calls: list[CallRecord], candidates: list[dict] | None = None, dead: bool = False) -> str:
    """No suggestion could be produced. Put the case in front of an evaluator anyway."""
    ev = Evaluation(
        transcript_course_id=course.id, syllabus_id=syllabus.id if syllabus else None,
        status=EvaluationStatus.NO_SUGGESTION, gaps=reason, validation_errors=[reason],
        candidates=candidates or [], prompt_version=PROMPT_VERSION, created_at=datetime.now(timezone.utc),
    )
    db.add(ev)
    db.flush()
    _save_calls(db, calls, job, ev)
    change_status(db, course, CourseStatus.IN_REVIEW, actor_type=ActorType.SYSTEM, actor_id=None,
                  message="Sent for review without an AI suggestion")
    (mark_dead if dead else lambda d, j, e: mark_done(d, j))(db, job, reason)
    write_audit(db, actor_type=ActorType.SYSTEM, actor_id=None, action="evaluation.no_suggestion",
                entity_type="evaluation", entity_id=ev.id, after={"reason": reason})
    db.commit()
    return "no_suggestion"


def process_job(db: Session, job: Job, *, gateway_factory: GatewayFactory = build_gateway,
                embedder: Embedder | None = None) -> str:
    course = db.get(TranscriptCourse, job.transcript_course_id)
    if course is None or course.status != CourseStatus.AI_PENDING:
        mark_done(db, job)
        db.commit()
        return "skipped"

    syllabus = db.scalar(select(Syllabus).where(Syllabus.transcript_course_id == course.id).order_by(Syllabus.version.desc()))
    calls: list[CallRecord] = []
    candidates_json: list[dict] = []
    try:
        if syllabus is None:
            return _hand_to_human(db, job, course, None, "No syllabus is on file", calls)

        # 1. read and safety check the file
        document = db.get(Document, syllabus.document_id)
        ext = PurePath(document.original_filename).suffix
        data = get_store().get(document.container, document.blob_path)
        if document.scan_status == ScanStatus.PENDING:
            safe, why = is_safe(data, ext)
            document.scan_status = ScanStatus.CLEAN if safe else ScanStatus.REJECTED
            db.commit()
            if not safe:
                return _hand_to_human(db, job, course, syllabus, f"The file was blocked by the safety check: {why}", calls)
        if document.scan_status != ScanStatus.CLEAN:
            return _hand_to_human(db, job, course, syllabus, "The file did not pass the safety check", calls)
        try:
            raw_text = extract_text(data, ext)
        except ExtractError as e:
            return _hand_to_human(db, job, course, syllabus, str(e), calls)

        # 2. remove personal details, keep the text, embed it
        clean_text = redact(raw_text)
        syllabus.extracted_text = clean_text[:50000]
        embedder = embedder or get_embedder()
        vector = embedder.embed([clean_text])[0]
        syllabus.embedding = vector
        db.commit()

        # 3. closest BU courses
        found = retrieve_by_vector(db, vector, k=5)
        candidates = [
            CandidateInfo(code=f.course.code, title=f.course.title, credits=f.course.credits,
                          description=f.course.description, outcomes=list(f.course.learning_outcomes or []), score=f.score)
            for f in found
        ]
        candidates_json = [{"code": c.code, "title": c.title, "score": c.score} for c in candidates]
        school = db.scalar(
            select(Institution.name).join(Transcript, Transcript.institution_id == Institution.id)
            .where(Transcript.id == course.transcript_id)
        ) or "Unknown school"
        credits = course.converted_credits if course.converted_credits is not None else course.raw_credits

        # 4. ask the gateway; every answer is checked before it is used
        req, _truncated = build_request(
            course_code=course.source_code, course_title=course.source_title, credits=credits,
            system_name=school, syllabus_text=clean_text, candidates=candidates,
        )
        validator = make_validator(candidates, clean_text, course_credits=credits)
        result = gateway_factory(on_call=calls.append).complete(req, validate=validator)
        suggestion = result.value

        # 5. instruction-like text in the syllabus lowers trust; the evaluator is told
        injected = detect_injection(raw_text)
        confidence = suggestion.confidence
        gaps = suggestion.gaps
        errors: list[str] = []
        if injected:
            confidence = min(confidence, INJECTION_CONFIDENCE_CAP)
            gaps = (gaps + " " if gaps else "") + "Warning: the syllabus contains instruction-like text. Treat this suggestion with caution."
            errors.append("possible prompt injection: " + "; ".join(injected))

        by_code = {c.code: f.course.id for c, f in zip(candidates, found)}
        ev = Evaluation(
            transcript_course_id=course.id, syllabus_id=syllabus.id, status=EvaluationStatus.COMPLETED,
            candidates=candidates_json, suggested_outcome=Outcome(suggestion.outcome),
            suggested_bu_course_id=by_code.get(suggestion.bu_course_code) if suggestion.bu_course_code else None,
            confidence=Decimal(str(round(confidence, 3))), evidence=suggestion.evidence, gaps=gaps,
            provider=result.response.provider, model=result.response.model,
            prompt_version=PROMPT_VERSION, validation_errors=errors, created_at=datetime.now(timezone.utc),
        )
        db.add(ev)
        db.flush()
        _save_calls(db, calls, job, ev)
        dept = department_for(suggestion.bu_course_code, course.source_code)
        change_status(db, course, CourseStatus.IN_REVIEW, actor_type=ActorType.AI, actor_id=None,
                      message=f"Sent to the {dept} department for review")
        mark_done(db, job)
        write_audit(db, actor_type=ActorType.AI, actor_id=None, action="evaluation.suggested",
                    entity_type="evaluation", entity_id=ev.id,
                    after={"outcome": suggestion.outcome, "bu_course": suggestion.bu_course_code,
                           "confidence": float(confidence), "provider": result.response.provider,
                           "fallback_used": result.fallback_used, "injection_flagged": bool(injected)})
        db.commit()
        return "suggested"

    except AllProvidersFailed as e:
        db.rollback()
        job = db.get(Job, job.id)
        course = db.get(TranscriptCourse, course.id)
        syllabus = db.get(Syllabus, syllabus.id) if syllabus else None
        if job.attempts >= settings.worker_max_attempts:
            return _hand_to_human(db, job, course, syllabus, "No AI provider was available. Review manually",
                                  calls, candidates_json, dead=True)
        _save_calls(db, calls, job, None)
        requeue(db, job, str(e))
        db.commit()
        log.warning("Job %s requeued: %s", job.id, e)
        return "requeued"

    except Exception as e:  # a bug or an unexpected failure must not strand the course
        db.rollback()
        job = db.get(Job, job.id)
        log.exception("Job %s failed", job.id)
        if job.attempts >= settings.worker_max_attempts:
            course = db.get(TranscriptCourse, course.id)
            return _hand_to_human(db, job, course, None, "The evaluation failed. Review manually", [], dead=True)
        requeue(db, job, f"{type(e).__name__}: {e}")
        db.commit()
        return "requeued"


def run_once(db: Session, worker_id: str = "worker", **kwargs) -> str | None:
    """Claim and process one job. Returns the outcome, or None when the queue is empty."""
    job = claim_job(db, worker_id)
    if job is None:
        return None
    if job.job_type == PARSE:
        from app.workers.transcript_worker import process_transcript_job  # imported here: that module imports this one

        kwargs.pop("embedder", None)  # parsing needs no embeddings
        return process_transcript_job(db, job, **kwargs)
    if job.transcript_course_id is None:
        return process_job(db, job, **kwargs)
    # The AI step runs inside the course's workflow, which then pauses for the evaluator.
    return run_evaluation(db, job.transcript_course_id, lambda: process_job(db, job, **kwargs))


def run_loop(stop: threading.Event, worker_id: str, interval: float = 5.0, once: bool = False) -> int:
    """Claim and process jobs until `stop` is set (or, with once=True, until the queue is empty).

    Used by the standalone worker and by the API when it runs the worker inside its own process.
    A failure (for example the database being briefly unreachable) is logged and retried after a
    pause; it never ends the loop. Returns how many jobs were handled.
    """
    handled = 0
    while not stop.is_set():
        try:
            with SessionLocal() as db:
                outcome = run_once(db, worker_id)
        except Exception:
            log.exception("Worker loop error; trying again in %.0f s", max(interval, 1.0))
            stop.wait(max(interval, 1.0))
            continue
        if outcome is None:
            if once:
                break
            stop.wait(interval)
        else:
            handled += 1
            log.info("Job finished: %s", outcome)
    return handled


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="process the queue once, then exit")
    ap.add_argument("--interval", type=float, default=5.0, help="seconds to wait when the queue is empty")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"

    gateway = build_gateway()
    if not gateway.providers:
        log.warning("No LLM provider is configured (set ANTHROPIC_API_KEY or GEMINI_API_KEY). "
                    "Jobs will be retried and then sent to evaluators without a suggestion.")
    log.info("Worker %s started. Providers: %s", worker_id, [p["provider"] for p in gateway.status()])

    stop = threading.Event()
    try:
        handled = run_loop(stop, worker_id, args.interval, once=args.once)
    except KeyboardInterrupt:
        stop.set()
        handled = 0
    log.info("Done. %d job(s) handled.", handled)


if __name__ == "__main__":
    main()
