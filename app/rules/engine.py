"""Deterministic transfer credit rules. Pure functions: no database, no AI.

Two stages:
  evaluate_course  checks the transcript line alone (grade, credits, deadline).
                   Runs when a transcript is processed.
  evaluate_target  checks a proposed BU equivalent against the program's rules.
                   Runs before a decision is recorded.

The AI can suggest anything; these checks are what stop credit being awarded.
"""

from collections.abc import Iterable
from datetime import date

from app.rules.credit_conversion import check_credits, convert_credits
from app.rules.deadline_rule import check_deadline
from app.rules.grade_rule import DEFAULT_MINIMUM, check_grade
from app.rules.hub_rule import hub_and_gpa_notes
from app.rules.program_rules import check_target
from app.rules.types import Check, CourseFacts, Evaluation, ProgramRule

# Bump when a rule changes, so stored results say which version produced them.
RULES_VERSION = "1.0.0"


def evaluate_course(
    facts: CourseFacts,
    *,
    bu_start_date: date | None,
    today: date,
    minimum_grade: str = DEFAULT_MINIMUM,
) -> Evaluation:
    checks: list[Check] = [
        check_grade(facts.grade, minimum_grade),
        check_credits(facts.raw_credits, facts.credit_system),
        check_deadline(bu_start_date, today),
        *hub_and_gpa_notes(),
    ]
    return Evaluation(checks=checks, converted_credits=convert_credits(facts.raw_credits, facts.credit_system))


def evaluate_target(
    bu_code: str,
    rules: Iterable[ProgramRule],
    already_transferred: Iterable[str] = (),
    must_take_at_bu: bool = False,
) -> Evaluation:
    return Evaluation(checks=check_target(bu_code, rules, already_transferred, must_take_at_bu))
