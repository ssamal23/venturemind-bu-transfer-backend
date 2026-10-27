"""Which academic department reviews a course. Derived from course codes."""

BU_PREFIX = {
    "CS": "Computer Science",
    "EC": "Economics",
    "PS": "Psychology",
    "BI": "Biology",
    "CH": "Chemistry",
    "MA": "Mathematics",
}

SOURCE_PREFIX = {
    "CS": "Computer Science",
    "CSCI": "Computer Science",
    "ECON": "Economics",
    "PSY": "Psychology",
    "BIO": "Biology",
    "CHEM": "Chemistry",
    "MATH": "Mathematics",
}


def department_for(bu_code: str | None, source_code: str) -> str:
    """BU course code ("CAS CH 101") wins; otherwise fall back to the source code ("BIO 201")."""
    if bu_code:
        parts = bu_code.split()
        if len(parts) >= 2 and parts[1] in BU_PREFIX:
            return BU_PREFIX[parts[1]]
    prefix = source_code.split()[0].split("-")[0].upper() if source_code else ""
    return SOURCE_PREFIX.get(prefix, "General")
