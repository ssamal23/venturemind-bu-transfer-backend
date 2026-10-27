"""Plain data types for the rules engine. No database or web code in here."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class Check:
    """One rule result, shown to students and evaluators.

    passed=False and note=False  -> blocks credit (a failure)
    passed=True  and note=True   -> information or a warning, never blocks
    passed=True  and note=False  -> a plain pass
    """

    rule: str
    label: str
    passed: bool
    note: bool = False

    @property
    def blocking(self) -> bool:
        return not self.passed and not self.note

    def to_json(self) -> dict[str, Any]:
        return {"rule": self.rule, "label": self.label, "pass": self.passed, "note": self.note}


@dataclass(frozen=True)
class CourseFacts:
    """What the transcript says about one course."""

    grade: str | None
    raw_credits: Decimal
    credit_system: str  # "SEMESTER" or "QUARTER"


@dataclass(frozen=True)
class ProgramRule:
    """A structured BU program rule (from program_requirements)."""

    type: str  # NON_TRANSFERABLE | MAX_TRANSFER | MUST_TAKE_AT_BU | REQUIRED_COURSE
    course_code: str | None = None
    group_label: str | None = None
    max_count: int | None = None
    min_level: int | None = None
    description: str = ""
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Evaluation:
    checks: list[Check]
    converted_credits: Decimal | None = None  # set by evaluate_course only

    @property
    def blocking(self) -> list[Check]:
        return [c for c in self.checks if c.blocking]

    @property
    def eligible(self) -> bool:
        return not self.blocking

    def to_json(self) -> list[dict[str, Any]]:
        return [c.to_json() for c in self.checks]
