"""One place that decides whether an uploaded file is acceptable. Syllabi and transcripts both use it."""

from dataclasses import dataclass
from pathlib import PurePath

from fastapi import HTTPException, UploadFile, status

from app.config import settings

# extension -> (content type, file signature). The type is checked against the bytes, not just the name.
ALLOWED = {
    ".pdf": ("application/pdf", (b"%PDF-",)),
    ".docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", (b"PK\x03\x04",)),
    ".doc": ("application/msword", (b"\xd0\xcf\x11\xe0",)),
}


@dataclass(frozen=True)
class ValidatedUpload:
    name: str
    extension: str
    content_type: str
    data: bytes


def read_validated_upload(file: UploadFile) -> ValidatedUpload:
    name = PurePath(file.filename or "").name
    ext = PurePath(name).suffix.lower()
    if ext not in ALLOWED:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Upload a PDF, DOC or DOCX file.")
    limit = settings.max_upload_mb * 1024 * 1024
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"The file is larger than {settings.max_upload_mb} MB.")
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The file is empty.")
    content_type, signatures = ALLOWED[ext]
    if not data.startswith(signatures):
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "The file content doesn't match its type.")
    return ValidatedUpload(name=name, extension=ext, content_type=content_type, data=data)
