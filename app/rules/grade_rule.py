from app.rules.types import Check

# Highest to lowest. A course earns credit at or above the minimum.
SCALE = ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-", "F"]
# Marks that mean no credit was earned.
NO_CREDIT_MARKS = {"W", "I", "IP", "NP", "NC", "U", "AU"}
# Pass/fail style marks: a person has to judge them.
JUDGEMENT_MARKS = {"P", "S", "CR", "PASS"}

DEFAULT_MINIMUM = "C"


def normalize(grade: str | None) -> str:
    return (grade or "").strip().upper().replace("−", "-").replace("–", "-")


def check_grade(grade: str | None, minimum: str = DEFAULT_MINIMUM) -> Check:
    """Many BU schools require a C or higher for transfer credit."""
    g = normalize(grade)
    if not g:
        return Check("grade", f"No grade found, so the {minimum} minimum can't be checked. Confirm it", True, note=True)
    if g in SCALE:
        if SCALE.index(g) <= SCALE.index(minimum):
            return Check("grade", f"Grade {g} meets the {minimum} minimum", True)
        return Check("grade", f"Grade {g} is below the {minimum} minimum", False)
    if g in NO_CREDIT_MARKS:
        return Check("grade", f"Grade {g} doesn't earn credit", False)
    if g in JUDGEMENT_MARKS:
        return Check("grade", f"Grade {g} can't be checked automatically. Confirm it meets the {minimum} minimum", True, note=True)
    return Check("grade", f"Grade {g} isn't recognized. Confirm it meets the {minimum} minimum", True, note=True)
