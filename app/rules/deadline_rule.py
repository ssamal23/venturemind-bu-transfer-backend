from datetime import date

from app.rules.types import Check

WARN_WITHIN_DAYS = 30


def deadline_for(start: date) -> date:
    """All credits must be transferred within one year of starting at BU."""
    try:
        return start.replace(year=start.year + 1)
    except ValueError:  # started on 29 February
        return start.replace(year=start.year + 1, day=28)


def check_deadline(start: date | None, today: date) -> Check:
    if start is None:
        return Check("deadline", "No BU start date on file, so the 1-year window can't be checked", True, note=True)
    deadline = deadline_for(start)
    days_left = (deadline - today).days
    if days_left < 0:
        return Check("deadline", f"The 1-year transfer window ended on {deadline.isoformat()}", False)
    if days_left <= WARN_WITHIN_DAYS:
        unit = "day" if days_left == 1 else "days"
        return Check("deadline", f"Only {days_left} {unit} left in the 1-year window", True, note=True)
    return Check("deadline", f"Within the 1-year window ({days_left} days left)", True)
