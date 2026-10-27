"""Reads an uploaded transcript into course rows.

Per job: safety check the file, extract the text, remove identity lines and personal
details, ask the LLM gateway for the course list, check every course against the text,
then create the course rows and run the rules (services/transcript_pipeline.py).

If the file can't be read, or no AI provider is available after the retries, the
transcript is marked FAILED with a message, so staff can enter it by hand. The student
is never left waiting, and nothing is invented.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import PurePath

from sqlalchemy.orm import Session

from app.config import settings
from app.core.audit import write_audit
from app.core.redaction import detect_injection
from app.core.storage import get_store
from app.db.models import ActorType, Document, Institution, Job, ParseStatus, ScanStatus, Transcript
from app.llm.gateway import AllProvidersFailed, LLMGateway, build_gateway
from app.llm.transcripts import build_request, make_validator
from app.services.doc_scan import is_safe
from app.services.text_extract import ExtractError, extract_text
from app.services.transcript_pipeline import apply_parsed_transcript
from app.workers.evaluation_worker import _save_calls
from app.workers.queue import mark_dead, mark_done, requeue

log = logging.getLogger("worker.transcript")
GatewayFactory = Callable[..., LLMGateway]


def _fail(db: Session, job: Job, transcript: Transcript, reason: str, calls: list, *, dead: bool = False) -> str:
    transcript.parse_status = ParseStatus.FAILED
    transcript.parse_error = reason
    _save_calls(db, calls, job, None)
    (mark_dead(db, job, reason) if dead else mark_done(db, job))
    write_audit(db, actor_type=ActorType.SYSTEM, actor_id=None, action="transcript.parse_failed",
                entity_type="transcript", entity_id=transcript.id, after={"reason": reason})
    db.commit()
    return "failed"


def process_transcript_job(db: Session, job: Job, *, gateway_factory: GatewayFactory = build_gateway) -> str:
    transcript = db.get(Transcript, job.transcript_id) if job.transcript_id else None
    if transcript is None or transcript.parse_status != ParseStatus.PENDING:
        mark_done(db, job)
        db.commit()
        return "skipped"

    calls: list = []
    try:
        document = db.get(Document, transcript.document_id)
        ext = PurePath(document.original_filename).suffix
        data = get_store().get(document.container, document.blob_path)
        if document.scan_status == ScanStatus.PENDING:
            safe, why = is_safe(data, ext)
            document.scan_status = ScanStatus.CLEAN if safe else ScanStatus.REJECTED
            db.commit()
            if not safe:
                return _fail(db, job, transcript, f"The file was blocked by the safety check: {why}", calls)
        if document.scan_status != ScanStatus.CLEAN:
            return _fail(db, job, transcript, "The file did not pass the safety check", calls)
        try:
            text = extract_text(data, ext)
        except ExtractError as e:
            return _fail(db, job, transcript, f"{e}. Staff will enter the courses by hand", calls)

        school = db.get(Institution, transcript.institution_id).name
        req, seen_text = build_request(text, school)
        result = gateway_factory(on_call=calls.append).complete(req, validate=make_validator(seen_text))

        counts = apply_parsed_transcript(db, transcript, result.value)
        injected = detect_injection(text)
        if injected:
            write_audit(db, actor_type=ActorType.SYSTEM, actor_id=None, action="transcript.injection_flagged",
                        entity_type="transcript", entity_id=transcript.id, after={"snippets": injected})
        _save_calls(db, calls, job, None)
        mark_done(db, job)
        db.commit()
        log.info("Transcript %s parsed: %s", transcript.id, counts)
        return "parsed"

    except AllProvidersFailed as e:
        db.rollback()
        job, transcript = db.get(Job, job.id), db.get(Transcript, transcript.id)
        if job.attempts >= settings.worker_max_attempts:
            return _fail(db, job, transcript, "No AI provider was available. Staff will enter the courses by hand", calls, dead=True)
        _save_calls(db, calls, job, None)
        requeue(db, job, str(e))
        db.commit()
        return "requeued"

    except Exception as e:
        db.rollback()
        job, transcript = db.get(Job, job.id), db.get(Transcript, transcript.id)
        log.exception("Transcript job %s failed", job.id)
        if job.attempts >= settings.worker_max_attempts:
            return _fail(db, job, transcript, "Reading the transcript failed. Staff will enter the courses by hand", [], dead=True)
        requeue(db, job, f"{type(e).__name__}: {e}")
        db.commit()
        return "requeued"
