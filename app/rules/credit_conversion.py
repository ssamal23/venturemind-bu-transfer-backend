from decimal import ROUND_DOWN, Decimal

from app.rules.types import Check

TWO_THIRDS = Decimal(2) / Decimal(3)
CENTS = Decimal("0.01")


def convert_credits(raw: Decimal, credit_system: str) -> Decimal:
    """Quarter credits convert at two-thirds of a semester credit.

    BU's own example: a 4-credit quarter course becomes 2.66, so the result is
    truncated to two decimals, not rounded (rounding would give 2.67).
    Semester credits are unchanged.
    """
    raw = Decimal(raw)
    if credit_system.upper() == "QUARTER":
        return (raw * TWO_THIRDS).quantize(CENTS, rounding=ROUND_DOWN)
    return raw.quantize(CENTS)


def check_credits(raw: Decimal, credit_system: str) -> Check:
    converted = convert_credits(raw, credit_system)
    if credit_system.upper() == "QUARTER":
        label = f"Quarter credits converted: {raw.normalize():f} to {converted}"
    else:
        label = "Semester credits, no conversion"
    return Check("credit_conversion", label, True)


def credits_for_bu_equivalent(credit_system: str, converted: Decimal | None, raw: Decimal, bu_credits: Decimal) -> Decimal:
    """Credits awarded when a course is matched to a BU course.

    Semester course: the BU course's credits. Quarter course: the converted credits, never
    more than the BU course carries (BU's brief: a 4-credit quarter course becomes 2.66).
    The sponsor has not confirmed this for equivalents; see docs/rules.md, assumption 8.
    """
    if credit_system.upper() == "QUARTER":
        return min(converted if converted is not None else convert_credits(raw, credit_system), bu_credits)
    return Decimal(bu_credits)
