import hashlib
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import write_audit
from app.core.auth import require_role
from app.core.storage import get_store
from app.core.uploads import read_validated_upload
from app.db.models import (
    ActorType,
    CourseStatus,
    DocType,
    Document,
    Job,
    JobStatus,
    Role,
    ScanStatus,
    Syllabus,
    Transcript,
    TranscriptCourse,
    TransferApplication,
    User,
)
from app.db.session import get_session
from app.schemas.transcript import SyllabusUploadOut
from app.services.status import change_status

router = APIRouter(tags=["syllabi"])

@router.post("/transcript-courses/{course_id}/syllabus", response_model=SyllabusUploadOut, status_code=201)
def upload_syllabus(
    course_id: uuid.UUID,
    file: UploadFile,
    user: User = Depends(require_role(Role.STUDENT)),
    db: Session = Depends(get_session),
) -> SyllabusUploadOut:
    row = db.execute(
        select(TranscriptCourse, TransferApplication.student_id)
        .join(Transcript, TranscriptCourse.transcript_id == Transcript.id)
        .join(TransferApplication, Transcript.application_id == TransferApplication.id)
        .where(TranscriptCourse.id == course_id)
    ).first()
    if row is None or row[1] != user.id:
        # Same answer for "missing" and "someone else's", so ids can't be probed.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Course not found.")
    course: TranscriptCourse = row[0]

    if course.status not in (CourseStatus.AWAITING_SYLLABUS, CourseStatus.PENDING_INFO):
        raise HTTPException(status.HTTP_409_CONFLICT, "This course isn't waiting for a syllabus.")

    upload = read_validated_upload(file)
    name, ext, content_type, data = upload.name, upload.extension, upload.content_type, upload.data

    doc_id = uuid.uuid4()
    blob_path = f"{course.id}/{doc_id}{ext}"
    get_store().put("syllabi", blob_path, data)

    document = Document(
        id=doc_id,
        doc_type=DocType.SYLLABUS,
        uploaded_by=user.id,
        container="syllabi",
        blob_path=blob_path,
        original_filename=name[:300],
        content_type=content_type,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        scan_status=ScanStatus.PENDING,  # workers only read CLEAN documents
    )
    db.add(document)
    db.flush()

    version = (db.scalar(select(func.max(Syllabus.version)).where(Syllabus.transcript_course_id == course.id)) or 0) + 1
    syllabus = Syllabus(transcript_course_id=course.id, document_id=document.id, version=version)
    db.add(syllabus)
    db.flush()

    db.add(
        Job(
            job_type="evaluate_course",
            transcript_course_id=course.id,
            status=JobStatus.QUEUED,
            idempotency_key=f"evaluate:{course.id}:v{version}",
        )
    )
    change_status(
        db,
        course,
        CourseStatus.AI_PENDING,
        actor_type=ActorType.STUDENT,
        actor_id=user.id,
        message=f"Syllabus uploaded ({name[:80]})",
    )
    write_audit(
        db,
        actor_type=ActorType.STUDENT,
        actor_id=user.id,
        action="syllabus.uploaded",
        entity_type="syllabus",
        entity_id=syllabus.id,
        after={"course_id": str(course.id), "version": version, "sha256": document.sha256},
    )
    db.commit()
    return SyllabusUploadOut(course_id=str(course.id), status=course.status.value, version=version)
