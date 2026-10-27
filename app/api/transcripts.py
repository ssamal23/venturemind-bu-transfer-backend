import hashlib
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import write_audit
from app.core.auth import require_role
from app.core.storage import get_store
from app.core.uploads import read_validated_upload
from app.db.models import (
    ActorType, ApplicationStatus, DocType, Document, Institution, Job, JobStatus, ParseStatus, Role,
    ScanStatus, Transcript, TranscriptCourse, TransferApplication, User,
)
from app.db.session import get_session
from app.schemas.transcript import TranscriptStatusOut, TranscriptUploadOut
from app.workers.queue import PARSE

router = APIRouter(tags=["transcripts"])


@router.post("/transcripts", response_model=TranscriptUploadOut, status_code=201)
def upload_transcript(
    institution_id: Annotated[uuid.UUID, Form(alias="institutionId")],
    file: UploadFile,
    user: User = Depends(require_role(Role.STUDENT)),
    db: Session = Depends(get_session),
) -> TranscriptUploadOut:
    school = db.get(Institution, institution_id)
    if school is None or school.is_bu:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Choose the school this transcript is from.")
    upload = read_validated_upload(file)
    digest = hashlib.sha256(upload.data).hexdigest()

    already = db.scalar(
        select(Document.id).where(
            Document.uploaded_by == user.id, Document.doc_type == DocType.TRANSCRIPT,
            Document.sha256 == digest, Document.deleted_at.is_(None),
        )
    )
    if already:
        raise HTTPException(status.HTTP_409_CONFLICT, "You have already uploaded this transcript.")

    # One open application per student. The first upload creates it.
    application = db.scalar(
        select(TransferApplication)
        .where(TransferApplication.student_id == user.id, TransferApplication.status != ApplicationStatus.CANCELLED)
        .order_by(TransferApplication.created_at.desc())
        .limit(1)
    )
    if application is None:
        application = TransferApplication(student_id=user.id, status=ApplicationStatus.SUBMITTED)
        db.add(application)
        db.flush()

    doc_id = uuid.uuid4()
    blob_path = f"{application.id}/{doc_id}{upload.extension}"
    get_store().put("transcripts", blob_path, upload.data)
    document = Document(
        id=doc_id, doc_type=DocType.TRANSCRIPT, uploaded_by=user.id, container="transcripts", blob_path=blob_path,
        original_filename=upload.name[:300], content_type=upload.content_type, size_bytes=len(upload.data),
        sha256=digest, scan_status=ScanStatus.PENDING,
    )
    db.add(document)
    db.flush()
    transcript = Transcript(
        application_id=application.id, institution_id=school.id, document_id=document.id, parse_status=ParseStatus.PENDING,
    )
    db.add(transcript)
    db.flush()
    db.add(Job(job_type=PARSE, transcript_id=transcript.id, status=JobStatus.QUEUED, idempotency_key=f"parse:{transcript.id}"))
    write_audit(db, actor_type=ActorType.STUDENT, actor_id=user.id, action="transcript.uploaded",
                entity_type="transcript", entity_id=transcript.id,
                after={"school": school.name, "sha256": digest, "size_bytes": len(upload.data)})
    db.commit()
    return TranscriptUploadOut(transcript_id=str(transcript.id), status=transcript.parse_status.value)


@router.get("/transcripts/{transcript_id}", response_model=TranscriptStatusOut)
def transcript_status(
    transcript_id: uuid.UUID,
    user: User = Depends(require_role(Role.STUDENT)),
    db: Session = Depends(get_session),
) -> TranscriptStatusOut:
    row = db.execute(
        select(Transcript, Institution.name, TransferApplication.student_id)
        .join(TransferApplication, Transcript.application_id == TransferApplication.id)
        .join(Institution, Transcript.institution_id == Institution.id)
        .where(Transcript.id == transcript_id)
    ).first()
    if row is None or row[2] != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Transcript not found.")  # same answer for someone else's
    transcript, school, _ = row
    count = db.scalar(select(func.count()).select_from(TranscriptCourse).where(TranscriptCourse.transcript_id == transcript.id))
    return TranscriptStatusOut(
        id=str(transcript.id), school=school, status=transcript.parse_status.value,
        error=transcript.parse_error, course_count=count or 0,
    )
