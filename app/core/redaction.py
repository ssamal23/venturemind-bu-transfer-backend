"""Strip personal details before text reaches a model or a log, and flag instruction-like text."""

import re

_PATTERNS = [
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[ID]"),  # US SSN shape
    (re.compile(r"(?i)\b(student|id|u)\s*(id|number|no\.?|#)?\s*[:#]?\s*[A-Z]?\d{6,10}\b"), "[ID]"),
    (re.compile(r"\b[A-Z]\d{8}\b"), "[ID]"),
    (re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)"), "[PHONE]"),
]


def redact(text: str) -> str:
    for pattern, token in _PATTERNS:
        text = pattern.sub(token, text)
    return text


# Phrases that talk to the model rather than describe a course. A syllabus has no reason to contain them.
_INJECTION = [
    r"ignore (all |any |the )?(previous|prior|above|earlier) (instructions|rules|prompts?)",
    r"disregard (all |any |the )?(previous|prior|above|earlier)",
    r"(system|developer) (prompt|message|instruction)",
    r"you are (now|actually) ",
    r"(approve|grant|award|give)\b.{0,40}\b(full|automatic|maximum)\b.{0,20}\bcredit",
    r"confidence\s*(of|=|:)\s*(1(\.0+)?|100\s*%)",
    r"respond (only )?with",
    r"new instructions",
    r"</?\s*(untrusted_syllabus|system|assistant)\b",
]
_INJECTION_RE = re.compile("|".join(f"(?:{p})" for p in _INJECTION), re.IGNORECASE | re.DOTALL)


def detect_injection(text: str) -> list[str]:
    """Return the instruction-like snippets found (empty list means none). A flag, not proof."""
    return [m.group(0).strip()[:80] for m in _INJECTION_RE.finditer(text)][:5]
