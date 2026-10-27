"""A basic safety check on uploaded documents.

This is NOT a malware scanner. It rejects files that carry active content a syllabus has no
reason to contain. In production, replace this with Microsoft Defender for Storage or ClamAV
and keep the same interface.
"""

import io
import zipfile

ACTIVE_PDF_MARKERS = (b"/JavaScript", b"/JS ", b"/Launch", b"/EmbeddedFile", b"/OpenAction")


def is_safe(data: bytes, extension: str) -> tuple[bool, str]:
    ext = extension.lower()
    if ext == ".pdf":
        for marker in ACTIVE_PDF_MARKERS:
            if marker in data:
                return False, f"PDF contains active content ({marker.decode().strip()})"
    elif ext == ".docx":
        try:
            names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        except zipfile.BadZipFile:
            return False, "DOCX is not a valid archive"
        if any("vbaProject" in n or n.lower().endswith(".exe") for n in names):
            return False, "DOCX contains macros or executables"
    return True, ""
