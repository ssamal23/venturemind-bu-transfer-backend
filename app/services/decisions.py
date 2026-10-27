"""Recording an evaluator's decision.

Two steps, so the same rules apply whichever path calls this:
  plan_decision    reads and checks everything; raises DecisionError; writes nothing
  record_decision  plans again on the live data, then writes the decision, the course's
                   final fields, the status change and the audit row

The decision endpoint plans first (so a bad request is refused before the workflow is
touched). The workflow's `record` node then calls record_decision, so a change that
happened in between is caught too. The caller commits.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import write_audit
from app.db.models import (
    ActorType,
    Course,
    CourseStatus,
    DecisionOutcome,
    Evaluation,
    EvaluationDecision,
    Institution,
    Outcome,
    TranscriptCourse,
)
from app.rules.credit_conversion import credits_for_bu_equivalent
from app.rules.engine import RULES_VERSION
from app.services.rules_service import evaluate_proposed_equivalent, evaluate_stored_course
from app.services.status import change_status

TIMELINE_MESSAGE = {
    DecisionOutcome.ELECTIVE: "Decision: elective credit",
    DecisionOutcome.NO_CREDIT: "Decision: no credit",
    DecisionOutcome.REQUEST_INFO: "More information requested",
}


class DecisionError(Exception):
    """The decision can't be recorded. status_code follows HTTP so the API can pass it on."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class DecisionRequest:
    evaluator_id: uuid.UUID
    outcome: str  # BU_EQUIVALENT | ELECTIVE | NO_CREDIT | REQUEST_INFO
    reason: str
    bu_course_code: str | None = None

    def to_state(self) -> dict[str, Any]:
        """Plain values only: this is stored in the workflow checkpoint."""
        return {"evaluator_id": str(self.evaluator_id), "outcome": self.outcome, "reason": self.reason,
                "bu_course_code": self.bu_course_code}

    @classmethod
    def from_state(cls, data: dict[str, Any]) -> "DecisionRequest":
        return cls(uuid.UUID(data["evaluator_id"]), data["outcome"], data["reason"], data.get("bu_course_code"))


@dataclass
class DecisionPlan:
    outcome: DecisionOutcome
    reason: str
    evaluation: Evaluation | None
    bu_course: Course | None
    credits: Decimal | None
    overrode_ai: bool
    rule_results: list[dict[str, Any]] | None


@dataclass(frozen=True)
class DecisionResult:
    course_id: str
    course_status: str
    credits_awarded: float | None

    def to_state(self) -> dict[str, Any]:
        return {"course_id": self.course_id, "course_status": self.course_status, "credits_awarded": self.credits_awarded}


def plan_decision(db: Session, course: TranscriptCourse, req: DecisionRequest) -> DecisionPlan:
    if course.status != CourseStatus.IN_REVIEW:
        raise DecisionError(409, "This course isn't waiting for review.")
    reason = req.reason.strip()
    if not reason:
        raise DecisionError(422, "Enter a reason for your decision.")
    outcome = DecisionOutcome(req.outcome)

    evaluation = db.scalar(
        select(Evaluation).where(Evaluation.transcript_course_id == course.id).order_by(Evaluation.created_at.desc())
    )

    bu_course: Course | None = None
    credits: Decimal | None = None
    if outcome == DecisionOutcome.BU_EQUIVALENT:
        if req.bu_course_code:
            bu_course = db.scalar(
                select(Course)
                .join(Institution, Course.institution_id == Institution.id)
                .where(Institution.is_bu.is_(True), Course.code == req.bu_course_code.strip())
            )
        elif evaluation and evaluation.suggested_bu_course_id:
            bu_course = db.get(Course, evaluation.suggested_bu_course_id)
        if bu_course is None:
            raise DecisionError(422, "Choose the BU course for this equivalency.")
        credits = credits_for_bu_equivalent(
            course.credit_system.value, course.converted_credits, course.raw_credits, bu_course.credits
        )
    elif outcome == DecisionOutcome.ELECTIVE:
        credits = course.converted_credits if course.converted_credits is not None else course.raw_credits
    elif outcome == DecisionOutcome.NO_CREDIT:
        credits = Decimal("0")

    # Rules run again here, in code, on live data. The AI cannot talk its way past them,
    # and stored results are never trusted.
    rule_results = None
    if outcome in (DecisionOutcome.BU_EQUIVALENT, DecisionOutcome.ELECTIVE):
        checks = list(evaluate_stored_course(db, course).checks)
        if bu_course is not None:
            checks += evaluate_proposed_equivalent(db, course, bu_course).checks
        blocking = [c.label for c in checks if c.blocking]
        if blocking:
            raise DecisionError(409, "Credit can't be awarded: " + "; ".join(blocking))
        rule_results = [c.to_json() for c in checks]

    overrode_ai = False
    if evaluation and evaluation.suggested_outcome and outcome != DecisionOutcome.REQUEST_INFO:
        overrode_ai = evaluation.suggested_outcome.value != outcome.value or (
            outcome == DecisionOutcome.BU_EQUIVALENT
            and bu_course is not None
            and bu_course.id != evaluation.suggested_bu_course_id
        )
    return DecisionPlan(outcome, reason, evaluation, bu_course, credits, overrode_ai, rule_results)


def record_decision(db: Session, course: TranscriptCourse, req: DecisionRequest) -> DecisionResult:
    plan = plan_decision(db, course, req)
    outcome, bu_course, credits = plan.outcome, plan.bu_course, plan.credits
    if plan.rule_results is not None:
        course.rule_results = plan.rule_results
        course.rules_version = RULES_VERSION

    decision = EvaluationDecision(
        transcript_course_id=course.id,
        evaluation_id=plan.evaluation.id if plan.evaluation else None,
        evaluator_id=req.evaluator_id,
        outcome=outcome,
        bu_course_id=bu_course.id if bu_course else None,
        credits_awarded=credits,
        reason=plan.reason,
        overrode_ai=plan.overrode_ai,
    )
    db.add(decision)
    db.flush()

    actor = dict(actor_type=ActorType.EVALUATOR, actor_id=req.evaluator_id)
    if outcome == DecisionOutcome.REQUEST_INFO:
        change_status(db, course, CourseStatus.PENDING_INFO, message=TIMELINE_MESSAGE[outcome],
                      note=f"More information requested: {plan.reason}", **actor)
    else:
        course.final_outcome = Outcome(outcome.value)
        course.final_bu_course_id = bu_course.id if bu_course else None
        course.final_credits = credits
        message = f"Decision: equivalent to {bu_course.code}" if bu_course else TIMELINE_MESSAGE[outcome]
        change_status(db, course, CourseStatus.DECIDED, message=message, **actor)

    write_audit(
        db, action="decision.recorded", entity_type="evaluation_decision", entity_id=decision.id,
        after={
            "course_id": str(course.id),
            "outcome": outcome.value,
            "bu_course": bu_course.code if bu_course else None,
            "credits": str(credits) if credits is not None else None,
            "overrode_ai": plan.overrode_ai,
        },
        **actor,
    )
    return DecisionResult(
        course_id=str(course.id),
        course_status=course.status.value,
        credits_awarded=float(credits) if credits is not None else None,
    )
