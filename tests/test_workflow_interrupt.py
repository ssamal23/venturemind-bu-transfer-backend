"""The per-course LangGraph workflow: it pauses for a human, survives a restart, and loops when asked."""

import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.api import decisions as decisions_api
from app.db import models as m
from app.llm.providers.testing import FailingProvider, FakeProvider
from app.services.decisions import DecisionRequest
from app.workers.evaluation_worker import run_once
from app.workflows import graph as wf
from app.workflows.nodes import Deps
from tests.test_worker import (GOOD_ANSWER, PSY_SYLLABUS, course_row, embedder, gateway_factory, job_row,  # noqa: F401
                               make_docx, psy_course, upload)

GOOD = FakeProvider("claude", reply=json.dumps(GOOD_ANSWER))


def status_of(course_id, saver):
    return wf.workflow_status(uuid.UUID(str(course_id)), saver)


def evaluate(client, student, db, psy_course, embedder, provider=None):
    """Upload a syllabus and run the worker once."""
    upload(client, student, psy_course, make_docx(PSY_SYLLABUS))
    return run_once(db, gateway_factory=gateway_factory(provider or FakeProvider("claude", reply=json.dumps(GOOD_ANSWER))), embedder=embedder)


def decide(client, evaluator, cid, **body):
    body.setdefault("reason", "Reviewed.")
    body.setdefault("outcome", "BU_EQUIVALENT")
    return client.post(f"/api/transcript-courses/{cid}/decision", headers=evaluator["headers"], json=body)


# ---------------------------------------------------------------- the pause
def test_the_ai_step_ends_at_a_human_pause_and_nothing_is_decided(client, student, db, psy_course, embedder, memory_checkpointer):
    assert evaluate(client, student, db, psy_course, embedder) == "suggested"
    st = status_of(psy_course, memory_checkpointer)
    assert st["state"] == "waiting_for_evaluator" and st["rounds"] == 1 and st["checkpointId"]
    assert course_row(db, psy_course).status == m.CourseStatus.IN_REVIEW
    assert course_row(db, psy_course).final_outcome is None
    assert db.scalar(select(m.EvaluationDecision).where(m.EvaluationDecision.transcript_course_id == psy_course)) is None


def test_the_paused_workflow_is_visible_to_evaluators_only(client, student, evaluator, db, psy_course, embedder):
    evaluate(client, student, db, psy_course, embedder)
    seen = client.get(f"/api/transcript-courses/{psy_course}/workflow", headers=evaluator["headers"])
    assert seen.status_code == 200 and seen.json()["state"] == "waiting_for_evaluator"
    assert client.get(f"/api/transcript-courses/{psy_course}/workflow", headers=student["headers"]).status_code == 403
    assert client.get(f"/api/transcript-courses/{psy_course}/workflow").status_code == 401


def test_nothing_runs_while_paused_and_a_duplicate_job_cannot_restart_it(client, student, db, psy_course, embedder, memory_checkpointer):
    evaluate(client, student, db, psy_course, embedder)
    assert run_once(db) is None  # the queue is empty; the course just waits
    db.add(m.Job(job_type="evaluate_course", transcript_course_id=psy_course, idempotency_key="dup-1"))
    db.flush()
    outcome = run_once(db, gateway_factory=gateway_factory(FakeProvider("claude", reply="{}")), embedder=embedder)
    assert outcome == "skipped"  # not AI_PENDING any more
    assert status_of(psy_course, memory_checkpointer)["state"] == "waiting_for_evaluator"  # still paused


def test_the_checkpoint_holds_flow_state_only(client, student, db, psy_course, embedder, memory_checkpointer):
    evaluate(client, student, db, psy_course, embedder)
    snap = wf.build_graph(Deps(), memory_checkpointer).get_state({"configurable": {"thread_id": str(psy_course)}})
    assert set(snap.values) <= {"course_id", "last_outcome", "rounds", "decision", "decision_result", "error", "error_status"}
    json.dumps(snap.values)  # plain values: no database objects, no student text
    assert snap.values["course_id"] == str(psy_course) and snap.next == ("await_review",)


def test_the_pause_survives_a_restart(client, student, evaluator, db, psy_course, embedder, memory_checkpointer):
    """A new process has a new graph object. All it shares is the saved checkpoint."""
    evaluate(client, student, db, psy_course, embedder)
    fresh_graph = wf.build_graph(Deps(), memory_checkpointer)  # nothing carried over from the worker's graph
    assert fresh_graph.get_state({"configurable": {"thread_id": str(psy_course)}}).next == ("await_review",)
    res = decide(client, evaluator, psy_course)
    assert res.status_code == 200 and res.json()["courseStatus"] == "DECIDED"


# ---------------------------------------------------------------- the decision resumes it
def test_a_decision_resumes_the_workflow_and_finishes_it(client, student, evaluator, db, psy_course, embedder, memory_checkpointer):
    evaluate(client, student, db, psy_course, embedder)
    res = decide(client, evaluator, psy_course)
    assert res.status_code == 200 and res.json()["creditsAwarded"] == 4
    assert course_row(db, psy_course).status == m.CourseStatus.DECIDED
    st = status_of(psy_course, memory_checkpointer)
    assert st["state"] == "finished"
    d = db.scalar(select(m.EvaluationDecision).where(m.EvaluationDecision.transcript_course_id == psy_course))
    assert d.reason == "Reviewed." and not d.overrode_ai
    assert decide(client, evaluator, psy_course).status_code == 409  # already decided


def test_a_refused_decision_leaves_the_workflow_paused(client, student, evaluator, db, psy_course, embedder, memory_checkpointer):
    evaluate(client, student, db, psy_course, embedder)
    course_row(db, psy_course).grade = "F"
    db.flush()
    refused = decide(client, evaluator, psy_course)
    assert refused.status_code == 409 and "below the C minimum" in refused.json()["detail"]
    assert status_of(psy_course, memory_checkpointer)["state"] == "waiting_for_evaluator"
    assert course_row(db, psy_course).status == m.CourseStatus.IN_REVIEW
    # a corrected decision works
    assert decide(client, evaluator, psy_course, outcome="NO_CREDIT", reason="Failing grade.").status_code == 200
    assert status_of(psy_course, memory_checkpointer)["state"] == "finished"


def test_the_record_step_rechecks_the_rules_even_if_the_endpoint_check_is_skipped(client, student, evaluator, db, psy_course, embedder,
                                                                                    memory_checkpointer, monkeypatch):
    """Defence in depth: something changed between the endpoint's check and the workflow's write."""
    evaluate(client, student, db, psy_course, embedder)
    course_row(db, psy_course).grade = "F"
    db.flush()
    monkeypatch.setattr(decisions_api, "plan_decision", lambda *a, **k: None)  # the endpoint's own check does nothing
    res = decide(client, evaluator, psy_course)
    assert res.status_code == 409 and "below the C minimum" in res.json()["detail"]  # the graph's record step caught it
    assert course_row(db, psy_course).final_outcome is None
    assert status_of(psy_course, memory_checkpointer)["state"] == "waiting_for_evaluator"
    assert db.scalar(select(m.EvaluationDecision).where(m.EvaluationDecision.transcript_course_id == psy_course)) is None


def test_the_graph_cannot_run_the_ai_step_outside_the_worker(db, psy_course, memory_checkpointer):
    with pytest.raises(RuntimeError, match="only run from the worker"):
        wf.build_graph(Deps(db=db), memory_checkpointer).invoke(
            {"course_id": str(psy_course)}, {"configurable": {"thread_id": "no-worker"}})


# ---------------------------------------------------------------- the second pause
def test_request_more_information_loops_through_the_student(client, student, evaluator, db, psy_course, embedder, memory_checkpointer):
    evaluate(client, student, db, psy_course, embedder)

    ask = decide(client, evaluator, psy_course, outcome="REQUEST_INFO", reason="Please add the lab syllabus.")
    assert ask.status_code == 200 and ask.json()["courseStatus"] == "PENDING_INFO"
    assert status_of(psy_course, memory_checkpointer)["state"] == "waiting_for_student"

    # the student replies with a better syllabus; the worker resumes the SAME workflow
    upload(client, student, psy_course, make_docx(PSY_SYLLABUS + "\nLaboratory: weekly 2 hour lab on research methods."))
    outcome = run_once(db, gateway_factory=gateway_factory(FakeProvider("claude", reply=json.dumps(GOOD_ANSWER))), embedder=embedder)
    assert outcome == "suggested"
    st = status_of(psy_course, memory_checkpointer)
    assert st["state"] == "waiting_for_evaluator" and st["rounds"] == 2
    evals = db.scalars(select(m.Evaluation).where(m.Evaluation.transcript_course_id == psy_course)).all()
    assert len(evals) == 2 and {e.syllabus_id for e in evals} != {None} and len({e.syllabus_id for e in evals}) == 2

    assert decide(client, evaluator, psy_course).status_code == 200
    assert status_of(psy_course, memory_checkpointer)["state"] == "finished"
    kinds = [d.outcome.value for d in db.scalars(select(m.EvaluationDecision).where(m.EvaluationDecision.transcript_course_id == psy_course).order_by(m.EvaluationDecision.decided_at))]
    assert kinds == ["REQUEST_INFO", "BU_EQUIVALENT"]  # both decisions are kept


# ---------------------------------------------------------------- outages
def test_when_no_ai_is_available_the_workflow_waits_for_the_ai_not_the_human(client, student, db, psy_course, embedder, memory_checkpointer):
    down = FailingProvider("claude")
    upload(client, student, psy_course, make_docx(PSY_SYLLABUS))
    assert run_once(db, gateway_factory=gateway_factory(down, max_attempts=1), embedder=embedder) == "requeued"
    assert status_of(psy_course, memory_checkpointer)["state"] == "waiting_for_ai"
    assert course_row(db, psy_course).status == m.CourseStatus.AI_PENDING

    job = job_row(db, psy_course)
    job.next_run_at -= timedelta(days=1)  # the retry is due, and Claude is back
    db.flush()
    assert run_once(db, gateway_factory=gateway_factory(FakeProvider("claude", reply=json.dumps(GOOD_ANSWER))), embedder=embedder) == "suggested"
    assert status_of(psy_course, memory_checkpointer)["state"] == "waiting_for_evaluator"


def test_with_no_suggestion_the_human_still_gets_the_pause(client, student, evaluator, db, psy_course, embedder, memory_checkpointer):
    upload(client, student, psy_course, make_docx("too short"))  # unreadable: no model call, goes to a person
    assert run_once(db, gateway_factory=gateway_factory(GOOD), embedder=embedder) == "no_suggestion"
    assert status_of(psy_course, memory_checkpointer)["state"] == "waiting_for_evaluator"
    assert decide(client, evaluator, psy_course, outcome="NO_CREDIT", reason="Cannot read the syllabus.").status_code == 200


# ---------------------------------------------------------------- courses without a saved workflow
def test_an_older_course_without_a_workflow_is_still_decided_with_the_same_checks(client, evaluator, db, memory_checkpointer):
    queue = client.get("/api/review-queue", headers=evaluator["headers"]).json()
    cid = next(c for c in queue if c["courseCode"] == "CS 2100")["id"]
    assert status_of(cid, memory_checkpointer)["state"] == "not_started"
    res = decide(client, evaluator, cid, buCourseCode="CAS CS 111")
    assert res.status_code == 200 and res.json()["creditsAwarded"] == 4


def test_parking_puts_an_existing_course_at_the_pause(client, evaluator, db, memory_checkpointer):
    queue = client.get("/api/review-queue", headers=evaluator["headers"]).json()
    cid = uuid.UUID(next(c for c in queue if c["courseCode"] == "CS 2100")["id"])
    assert wf.park_at_review(cid, memory_checkpointer) is True
    assert wf.park_at_review(cid, memory_checkpointer) is False  # already has a workflow
    assert status_of(cid, memory_checkpointer)["state"] == "waiting_for_evaluator"
    assert decide(client, evaluator, str(cid), buCourseCode="CAS CS 111").status_code == 200
    assert status_of(cid, memory_checkpointer)["state"] == "finished"


def test_parking_at_the_student_step(memory_checkpointer):
    cid = uuid.uuid4()
    assert wf.park_at_syllabus(cid, memory_checkpointer) is True
    assert status_of(cid, memory_checkpointer)["state"] == "waiting_for_student"


# ---------------------------------------------------------------- the graph itself
def test_the_graph_has_the_expected_shape():
    text = wf.diagram()
    for node in ("evaluate", "await_review", "record", "await_syllabus"):
        assert node in text
    assert "await_review --> record" in text and "await_syllabus --> evaluate" in text


def test_the_decision_payload_round_trips_through_the_checkpoint():
    req = DecisionRequest(uuid.uuid4(), "ELECTIVE", "Reason", None)
    assert DecisionRequest.from_state(json.loads(json.dumps(req.to_state()))) == req
