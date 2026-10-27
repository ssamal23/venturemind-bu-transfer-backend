from typing import Any, TypedDict


class CourseState(TypedDict, total=False):
    """Flow-control state stored in the checkpoint. Plain values only.

    The Postgres tables stay the system of record (statuses, evaluations, decisions).
    This only remembers where the course is in the workflow.
    """

    course_id: str
    last_outcome: str | None  # result of the latest AI step: suggested | no_suggestion | requeued | skipped
    rounds: int  # how many times the AI step has run (more than one after a request for more information)
    decision: dict[str, Any] | None  # the evaluator's decision, as submitted
    decision_result: dict[str, Any] | None
    error: str | None  # set when a submitted decision was refused; the workflow returns to the review pause
    error_status: int | None
