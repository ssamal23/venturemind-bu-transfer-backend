"""The per-course workflow, as a LangGraph graph with two human pauses.

    START -> evaluate -> await_review (PAUSE: evaluator) -> record -> END
                              ^                                |
                              |  decision refused -------------+
                              |
         evaluate <- await_syllabus (PAUSE: student) <- "request more information"

* evaluate        the AI step. It only ever writes a suggestion.
* await_review    the graph stops here and saves its state. No code runs until an
                  evaluator submits a decision, however long that takes, even across a restart.
* record          writes the decision, the status change and the audit row, after the
                  rules have been checked again in code.
* await_syllabus  the second pause, for the student.

Checkpoints say where each course is. The database tables say what is true.
"""

from __future__ import annotations

import uuid
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from sqlalchemy.orm import Session

from app.db.models import TranscriptCourse
from app.services.decisions import DecisionError, DecisionRequest, DecisionResult, record_decision
from app.workflows.checkpointer import get_checkpointer
from app.workflows.nodes import Deps, make_nodes
from app.workflows.state import CourseState

AWAITING_EVALUATOR = "await_review"
AWAITING_STUDENT = "await_syllabus"
AI_DONE = ("suggested", "no_suggestion")  # outcomes that move on to the human review pause


def build_graph(deps: Deps, checkpointer: BaseCheckpointSaver | None = None):
    nodes = make_nodes(deps)
    g = StateGraph(CourseState)
    for name, fn in nodes.items():
        g.add_node(name, fn)
    g.add_edge(START, "evaluate")
    # An AI step that was requeued (providers down) or skipped ends this run; the job retries later.
    g.add_conditional_edges("evaluate", lambda s: "await_review" if s.get("last_outcome") in AI_DONE else END,
                            {"await_review": "await_review", END: END})
    g.add_edge("await_review", "record")

    def after_record(state: CourseState) -> str:
        if state.get("error"):
            return "await_review"  # refused: back to the pause for a corrected decision
        if (state.get("decision") or {}).get("outcome") == "REQUEST_INFO":
            return "await_syllabus"
        return END

    g.add_conditional_edges("record", after_record,
                            {"await_review": "await_review", "await_syllabus": "await_syllabus", END: END})
    g.add_edge("await_syllabus", "evaluate")
    return g.compile(checkpointer=checkpointer or get_checkpointer())


def _cfg(course_id: uuid.UUID | str) -> dict[str, Any]:
    return {"configurable": {"thread_id": str(course_id)}}


def _next(graph, course_id) -> str | None:
    snap = graph.get_state(_cfg(course_id))
    return snap.next[0] if snap.next else None


# ---------------------------------------------------------------- worker side
def run_evaluation(db: Session, course_id: uuid.UUID, evaluate, checkpointer: BaseCheckpointSaver | None = None) -> str:
    """Run (or resume) the course's workflow for an evaluation job. Returns the AI step's outcome.

    `evaluate` is called with no arguments and returns the outcome string.
    """
    graph = build_graph(Deps(db=db, evaluate=evaluate), checkpointer)
    waiting = _next(graph, course_id)
    if waiting == AWAITING_EVALUATOR:
        # Already paused for a decision. A second job must not restart the workflow.
        return evaluate()
    if waiting == AWAITING_STUDENT:
        out = graph.invoke(Command(resume={"resumed_by": "syllabus_upload"}), _cfg(course_id))
    else:
        out = graph.invoke({"course_id": str(course_id), "decision": None, "error": None}, _cfg(course_id))
    return out.get("last_outcome") or "skipped"


# ---------------------------------------------------------------- API side
def submit_decision(db: Session, course: TranscriptCourse, req: DecisionRequest,
                    checkpointer: BaseCheckpointSaver | None = None) -> DecisionResult:
    """Hand the evaluator's decision to the paused workflow. The caller commits.

    A course with no saved workflow (older data) is decided directly, with the same checks.
    """
    graph = build_graph(Deps(db=db), checkpointer)
    if _next(graph, course.id) != AWAITING_EVALUATOR:
        return record_decision(db, course, req)
    out = graph.invoke(Command(resume=req.to_state()), _cfg(course.id))
    if out.get("error"):
        raise DecisionError(out.get("error_status") or 409, out["error"])
    return DecisionResult(**out["decision_result"])


# ---------------------------------------------------------------- inspection and repair
def park_at_review(course_id: uuid.UUID, checkpointer: BaseCheckpointSaver | None = None, rounds: int = 1) -> bool:
    """Put a course that is already in review at the pause, without running the AI step again.

    Used for demo data and to repair courses whose workflow was never saved. Returns False if
    the course already has a workflow.
    """
    graph = build_graph(Deps(), checkpointer)
    if graph.get_state(_cfg(course_id)).values:
        return False
    graph.update_state(
        _cfg(course_id),
        {"course_id": str(course_id), "last_outcome": "suggested", "rounds": rounds, "decision": None, "error": None},
        as_node="evaluate",
    )
    return True


def park_at_syllabus(course_id: uuid.UUID, checkpointer: BaseCheckpointSaver | None = None, rounds: int = 1) -> bool:
    """Put a course that is waiting for a new syllabus (after a request for more information) at the student pause."""
    graph = build_graph(Deps(), checkpointer)
    if graph.get_state(_cfg(course_id)).values:
        return False
    graph.update_state(
        _cfg(course_id),
        {"course_id": str(course_id), "last_outcome": "suggested", "rounds": rounds, "error": None,
         "decision": {"outcome": "REQUEST_INFO"}},
        as_node="record",
    )
    return True


def workflow_status(course_id: uuid.UUID, checkpointer: BaseCheckpointSaver | None = None) -> dict[str, Any]:
    graph = build_graph(Deps(), checkpointer)
    snap = graph.get_state(_cfg(course_id))
    if not snap.values:
        return {"state": "not_started", "rounds": 0, "checkpointId": None, "savedAt": None}
    waiting = snap.next[0] if snap.next else None
    if waiting is not None:
        state = {AWAITING_EVALUATOR: "waiting_for_evaluator", AWAITING_STUDENT: "waiting_for_student"}[waiting]
    elif snap.values.get("decision_result"):
        state = "finished"
    elif snap.values.get("last_outcome") == "requeued":
        state = "waiting_for_ai"  # no AI provider was available; the job will try again
    else:
        state = "idle"
    return {
        "state": state,
        "rounds": snap.values.get("rounds", 0),
        "checkpointId": snap.config["configurable"].get("checkpoint_id"),
        "savedAt": snap.created_at,
    }


def diagram() -> str:
    """The graph as Mermaid text, for the docs."""
    from langgraph.checkpoint.memory import InMemorySaver

    return build_graph(Deps(), InMemorySaver()).get_graph().draw_mermaid()
