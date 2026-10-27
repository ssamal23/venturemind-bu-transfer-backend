"""Rules that depend on which BU course is proposed as the equivalent."""

import re
from collections.abc import Iterable

from app.rules.types import Check, ProgramRule


def course_level(code: str) -> int | None:
    """"CAS EC 201" -> 201. None when the code has no number."""
    numbers = re.findall(r"\d+", code)
    return int(numbers[-1]) if numbers else None


def _dept_and_number(code: str) -> tuple[str, int | None]:
    parts = code.split()
    return (parts[-2] if len(parts) >= 2 else ""), course_level(code)


def check_target(
    bu_code: str,
    rules: Iterable[ProgramRule],
    already_transferred: Iterable[str] = (),
    must_take_at_bu: bool = False,
) -> list[Check]:
    """Check a proposed BU equivalent against its program's rules.

    already_transferred: BU course codes this student has already been credited with.
    must_take_at_bu: the equivalency database marks this BU course "must be taken at BU".
    """
    checks: list[Check] = []
    transferred = list(already_transferred)

    if must_take_at_bu:
        checks.append(Check("residency", f"BU requires {bu_code} to be taken in residence", False))

    for rule in rules:
        if rule.type == "NON_TRANSFERABLE" and rule.course_code == bu_code:
            where = f" ({rule.group_label})" if rule.group_label else ""
            checks.append(Check("non_transferable", f"{bu_code}{where} can't be transferred", False))

        elif rule.type == "MAX_TRANSFER" and rule.max_count is not None:
            level = course_level(bu_code)
            floor = rule.min_level or 0
            if level is not None and level >= floor:
                used = sum(1 for c in transferred if (course_level(c) or 0) >= floor and c != bu_code)
                if used >= rule.max_count:
                    checks.append(Check(
                        "max_transfer",
                        f"The limit of {rule.max_count} transferred courses at the {floor} level or above is already reached",
                        False,
                    ))
                else:
                    checks.append(Check(
                        "max_transfer",
                        f"{used + 1} of {rule.max_count} allowed transfers at the {floor} level or above",
                        True,
                        note=True,
                    ))

        elif rule.type == "MUST_TAKE_AT_BU":
            prefix = rule.params.get("course_prefix")
            beyond = {b.upper() for b in rule.params.get("beyond", [])}
            dept, number = _dept_and_number(bu_code)
            if prefix and dept.upper() == prefix.upper() and f"{dept} {number}".upper() not in beyond:
                checks.append(Check(
                    "residency_count",
                    f"BU requires at least {rule.max_count} {prefix} courses beyond {'/'.join(sorted(beyond))} in residence. "
                    "Confirm this course isn't counted toward them",
                    True,
                    note=True,
                ))
    return checks
