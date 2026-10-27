"""The four steps of the per-course workflow."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass

from langgraph.types import interrupt
from sqlalchemy.orm import Session

from app.db.models import TranscriptCourse
from app.services.decisions import DecisionError, DecisionRequest, record_decision
from app.workflows.state import CourseState

EVALUATOR_PAUSE = "evaluator_decision"
STUDENT_PAUSE = "student_syllabus"


@dataclass
class Deps:
    """What the nodes may use. Built fresh for each run, so it is never stored in a checkpoint."""

    db: Session | None = None
    evaluate: Callable[[], str] | None = None  # runs the AI step; returns suggested | no_suggestion | requeued | skipped


def make_nodes(deps: Deps) -> dict[str, Callable[[CourseState], dict]]:
    def evaluate(state: CourseState) -> dict:
        """The AI step: read the syllabus, find candidates, ask the gateway, save a SUGGESTION."""
        if deps.evaluate is None:
            raise RuntimeError("The AI step can only run from the worker")
        outcome = deps.evaluate()
        return {"last_outcome": outcome, "rounds": state.get("rounds", 0) + 1, "error": None, "error_status": None}

    def await_review(state: CourseState) -> dict:
        """Pause until an evaluator decides. Nothing runs while the course waits."""
        decision = interrupt({"waiting_for": EVALUATOR_PAUSE, "course_id": state["course_id"]})
        return {"decision": decision, "error": None, "error_status": None}

    def record(state: CourseState) -> dict:
        """Record the decision. Only an evaluator's submitted decision ever reaches here."""
        assert deps.db is not None
        course = deps.db.get(TranscriptCourse, uuid.UUID(state["course_id"]))
        try:
            result = record_decision(deps.db, course, DecisionRequest.from_state(state["decision"]))
        except DecisionError as e:
            # Refused (rules, state, missing reason). The course stays paused for a corrected decision.
            return {"error": e.detail, "error_status": e.status_code, "decision": None, "decision_result": None}
        return {"decision_result": result.to_state(), "error": None, "error_status": None}

    def await_syllabus(state: CourseState) -> dict:
        """The evaluator asked for more information. Pause until the student uploads a new syllabus."""
        interrupt({"waiting_for": STUDENT_PAUSE, "course_id": state["course_id"]})
        return {}

    return {"evaluate": evaluate, "await_review": await_review, "record": record, "await_syllabus": await_syllabus}
