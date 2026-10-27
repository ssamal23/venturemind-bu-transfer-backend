"""Read text out of uploaded syllabi. Extracted text is treated as untrusted data everywhere."""

import io
import re
import zipfile
from xml.etree import ElementTree

from pypdf import PdfReader

MAX_PAGES = 40
MIN_CHARS = 80  # below this the file is probably a scan with no text layer


class ExtractError(Exception):
    """The file couldn't be read. The message is safe to show to staff."""


def extract_text(data: bytes, extension: str) -> str:
    ext = extension.lower()
    if ext == ".pdf":
        text = _pdf(data)
    elif ext == ".docx":
        text = _docx(data)
    elif ext == ".doc":
        raise ExtractError("Old .doc files can't be read automatically. Ask the student for a PDF or DOCX")
    else:
        raise ExtractError(f"Unsupported file type {ext}")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < MIN_CHARS:
        raise ExtractError("The file has little or no readable text (it may be a scan). Review it manually")
    return text


def _pdf(data: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ExtractError("The PDF is password protected")
        return "\n".join((page.extract_text() or "") for page in reader.pages[:MAX_PAGES])
    except ExtractError:
        raise
    except Exception as e:
        raise ExtractError("The PDF couldn't be read") from e


def _docx(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as e:
        raise ExtractError("The DOCX couldn't be read") from e
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    root = ElementTree.fromstring(xml)
    paragraphs = []
    for p in root.iter(f"{ns}p"):
        paragraphs.append("".join(t.text or "" for t in p.iter(f"{ns}t")))
    return "\n".join(paragraphs)
