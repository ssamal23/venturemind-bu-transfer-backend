"""The credit office: the queue, posting one or many results, and the safeguards around the record."""

import json
import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import text, select

from app.db import models as m
from app.llm.providers.testing import FakeProvider
from app.workers.evaluation_worker import run_once
from app.workflows import graph as wf
from tests.conftest import login
from tests.test_worker import GOOD_ANSWER, PSY_SYLLABUS, embedder, gateway_factory, make_docx, psy_course, upload  # noqa: F401


@pytest.fixture()
def office(client):
    return login(client, "office@demo.edu")


def queue(client, office):
    res = client.get("/api/postings/queue", headers=office["headers"])
    assert res.status_code == 200, res.text
    return {c["courseCode"]: c for c in res.json()}


def post(client, office, course_id, **headers):
    return client.post(f"/api/transcript-courses/{course_id}/post", headers={**office["headers"], **headers})


def course_id(db, code):
    return db.scalar(select(m.TranscriptCourse.id).where(m.TranscriptCourse.source_code == code, m.TranscriptCourse.final_outcome.is_not(None)))


# ---------------------------------------------------------------- access
def test_the_credit_office_can_sign_in(client):
    res = client.post("/api/auth/login", json={"email": "office@demo.edu", "password": "demo1234"})
    assert res.status_code == 200 and res.json()["user"]["role"] == "credit_office"


@pytest.mark.parametrize("method, url", [
    ("get", "/api/postings/queue"), ("get", "/api/postings"),
    ("post", f"/api/transcript-courses/{uuid.uuid4()}/post"), ("post", "/api/postings/batch"),
])
def test_only_the_credit_office_can_use_the_posting_endpoints(client, student, evaluator, method, url):
    body = {"json": {"courseIds": [str(uuid.uuid4())]}} if url.endswith("batch") else {}
    for who in (student, evaluator):
        assert getattr(client, method)(url, headers=who["headers"], **body).status_code == 403
    assert getattr(client, method)(url, **body).status_code == 401


# ---------------------------------------------------------------- the queue
def test_the_queue_lists_decided_and_matched_courses_only(client, office):
    q = queue(client, office)
    assert set(q) == {"CS 2000", "ECON 201", "ENG 110"}  # not CHEM 110 (in review), PSY 200 (waiting), BIO 150 (not eligible)

    existing = q["CS 2000"]
    assert (existing["result"], existing["credits"], existing["source"]) == ("CAS CS 111", 4.0, "EXISTING_EQUIVALENCY")
    assert existing["decidedBy"] is None and existing["blockers"] == []
    assert q["ECON 201"]["credits"] == 2.66 and q["ECON 201"]["result"] == "CAS EC 101"

    decided = q["ENG 110"]
    assert (decided["result"], decided["outcome"], decided["credits"]) == ("Elective credit", "ELECTIVE", 3.0)
    assert decided["source"] == "DEPARTMENT_DECISION" and decided["decidedBy"] == "Dr. Rivera"
    assert "elective credit is awarded" in decided["reason"]


def test_a_decision_makes_the_course_appear_in_the_queue(client, office, evaluator, db):
    assert "CHEM 110" not in queue(client, office)
    cid = next(c for c in client.get("/api/review-queue", headers=evaluator["headers"]).json() if c["courseCode"] == "CHEM 110")["id"]
    assert client.post(f"/api/transcript-courses/{cid}/decision", headers=evaluator["headers"],
                       json={"outcome": "BU_EQUIVALENT", "reason": "Covers CH 101."}).status_code == 200
    row = queue(client, office)["CHEM 110"]
    assert row["result"] == "CAS CH 101" and row["credits"] == 4.0 and row["decidedBy"] == "Dr. Rivera"


def test_the_queue_flags_courses_that_would_fail_a_rule_now(client, office, db):
    app = db.scalar(select(m.TransferApplication).join(m.User).where(m.User.email == "student@demo.edu"))
    app.bu_start_date = date.today() - timedelta(days=500)  # the one-year window has closed
    db.flush()
    q = queue(client, office)
    assert any("1-year transfer window ended" in b for b in q["CS 2000"]["blockers"])
    assert q["ENG 110"]["blockers"]


# ---------------------------------------------------------------- posting one course
def test_posting_an_existing_equivalency(client, office, student, db):
    cid = course_id(db, "CS 2000")
    res = post(client, office, cid)
    assert res.status_code == 200
    assert res.json() | {"postingId": None} == {"postingId": None, "courseId": str(cid), "result": "CAS CS 111",
                                                  "creditsPosted": 4.0, "gradePosted": "T", "alreadyPosted": False}

    p = db.scalar(select(m.Posting).where(m.Posting.transcript_course_id == cid))
    assert p.grade_posted == "T" and p.credits_posted == 4 and p.decision_id is None  # no decision: it was an existing match
    assert p.outcome == m.Outcome.BU_EQUIVALENT and p.bu_course_id is not None and p.idempotency_key == f"post:{cid}"
    assert db.get(m.User, p.posted_by).email == "office@demo.edu"
    assert db.get(m.TranscriptCourse, cid).status == m.CourseStatus.POSTED
    assert queue(client, office).keys() == {"ECON 201", "ENG 110"}  # it left the queue


def test_posting_a_department_decision_links_the_decision(client, office, db):
    cid = course_id(db, "ENG 110")
    assert post(client, office, cid).json()["result"] == "Elective credit"
    p = db.scalar(select(m.Posting).where(m.Posting.transcript_course_id == cid))
    decision = db.get(m.EvaluationDecision, p.decision_id)
    assert decision.outcome == m.DecisionOutcome.ELECTIVE and p.outcome == m.Outcome.ELECTIVE and p.bu_course_id is None


def test_a_quarter_course_posts_its_converted_credits(client, office, db):
    res = post(client, office, course_id(db, "ECON 201"))
    assert res.json()["creditsPosted"] == 2.66


def test_posting_shows_students_a_posted_status_and_a_timeline_entry(client, office, student, db):
    post(client, office, course_id(db, "CS 2000"))
    data = client.get(f"/api/students/{student['user']['id']}/dashboard", headers=student["headers"]).json()
    cs = next(c for c in data["courses"] if c["sourceCode"] == "CS 2000")
    assert cs["status"] == "POSTED" and cs["note"] == "Posted to your academic record."
    assert cs["timeline"][-1]["label"] == "Posted to your academic record: CAS CS 111, 4 credits, grade T"


def test_posting_is_audited(client, office, db):
    post(client, office, course_id(db, "CS 2000"))
    entry = db.scalar(select(m.AuditLog).where(m.AuditLog.action == "posting.created"))
    assert entry.actor_type == m.ActorType.CREDIT_OFFICE and entry.after["grade"] == "T" and entry.after["source"] == "existing_equivalency"
    assert "course.status_changed" in [a.action for a in db.scalars(select(m.AuditLog))]


def test_a_no_credit_decision_can_be_posted_with_zero_credits(client, office, evaluator, db):
    cid = next(c for c in client.get("/api/review-queue", headers=evaluator["headers"]).json() if c["courseCode"] == "CHEM 112")["id"]
    client.post(f"/api/transcript-courses/{cid}/decision", headers=evaluator["headers"], json={"outcome": "NO_CREDIT", "reason": "No overlap."})
    assert queue(client, office)["CHEM 112"]["credits"] == 0
    res = post(client, office, cid)
    assert res.status_code == 200 and res.json()["creditsPosted"] == 0 and res.json()["result"] == "No credit"


# ---------------------------------------------------------------- safeguards
def test_posting_twice_returns_the_first_posting_and_writes_no_second(client, office, db):
    cid = course_id(db, "CS 2000")
    first = post(client, office, cid).json()
    again = post(client, office, cid)
    assert again.status_code == 200 and again.json()["alreadyPosted"] is True and again.json()["postingId"] == first["postingId"]
    assert len(db.scalars(select(m.Posting).where(m.Posting.transcript_course_id == cid)).all()) == 1


def test_an_idempotency_key_is_stored_and_cannot_be_reused_for_another_course(client, office, db):
    assert post(client, office, course_id(db, "CS 2000"), **{"Idempotency-Key": "batch-001"}).status_code == 200
    assert db.scalar(select(m.Posting.idempotency_key).where(m.Posting.idempotency_key == "batch-001")) == "batch-001"
    clash = post(client, office, course_id(db, "ENG 110"), **{"Idempotency-Key": "batch-001"})
    assert clash.status_code == 409 and "idempotency key" in clash.json()["detail"]


@pytest.mark.parametrize("code, why", [("PSY 200", "isn't ready"), ("BIO 150", "isn't ready"), ("CHEM 110", "isn't ready")])
def test_a_course_that_has_no_final_result_cannot_be_posted(client, office, db, code, why):
    cid = db.scalar(select(m.TranscriptCourse.id).where(m.TranscriptCourse.source_code == code))
    res = post(client, office, cid)
    assert res.status_code == 409 and why in res.json()["detail"]
    assert db.scalar(select(m.Posting).where(m.Posting.transcript_course_id == cid)) is None


def test_an_unknown_course_is_not_found(client, office):
    assert post(client, office, uuid.uuid4()).status_code == 404


def test_the_rules_are_checked_again_before_posting(client, office, db):
    cid = course_id(db, "CS 2000")
    db.get(m.TranscriptCourse, cid).grade = "F"  # changed after the equivalency was applied
    db.flush()
    res = post(client, office, cid)
    assert res.status_code == 409 and "below the C minimum" in res.json()["detail"]
    assert db.get(m.TranscriptCourse, cid).status == m.CourseStatus.MATCHED
    assert db.scalar(select(m.Posting).where(m.Posting.transcript_course_id == cid)) is None


def test_a_closed_transfer_window_blocks_posting(client, office, db):
    app = db.scalar(select(m.TransferApplication).join(m.User).where(m.User.email == "student@demo.edu"))
    app.bu_start_date = date.today() - timedelta(days=500)
    db.flush()
    res = post(client, office, course_id(db, "ENG 110"))
    assert res.status_code == 409 and "1-year transfer window ended" in res.json()["detail"]


def test_a_posting_cannot_be_edited_or_deleted(client, office, db):
    post(client, office, course_id(db, "CS 2000"))
    for statement in ("update postings set credits_posted = 99", "delete from postings"):
        with pytest.raises(Exception, match="append-only"):
            with db.begin_nested():
                db.execute(text(statement))
    assert db.scalar(select(m.Posting.credits_posted)) == 4


def test_a_decided_course_still_finishes_its_workflow_and_posting_is_separate(client, office, evaluator, db, memory_checkpointer):
    cid = next(c for c in client.get("/api/review-queue", headers=evaluator["headers"]).json() if c["courseCode"] == "CHEM 110")["id"]
    wf.park_at_review(uuid.UUID(cid), memory_checkpointer)
    client.post(f"/api/transcript-courses/{cid}/decision", headers=evaluator["headers"], json={"outcome": "ELECTIVE", "reason": "General credit."})
    assert wf.workflow_status(uuid.UUID(cid), memory_checkpointer)["state"] == "finished"
    assert db.get(m.TranscriptCourse, uuid.UUID(cid)).status == m.CourseStatus.DECIDED  # decided, not yet posted
    assert post(client, office, cid).status_code == 200


# ---------------------------------------------------------------- batch
def test_batch_posting_reports_each_course_separately(client, office, db):
    ids = [course_id(db, c) for c in ("CS 2000", "ECON 201", "ENG 110")]
    not_ready = db.scalar(select(m.TranscriptCourse.id).where(m.TranscriptCourse.source_code == "PSY 200"))
    res = client.post("/api/postings/batch", headers=office["headers"],
                      json={"courseIds": [str(i) for i in [*ids, not_ready, ids[0]]]})  # includes a repeat and one not ready
    assert res.status_code == 200
    body = res.json()
    results = {i["courseId"]: i for i in body["items"]}
    assert len(body["items"]) == 4  # the repeated id is dropped
    assert [results[str(i)]["result"] for i in ids] == ["posted"] * 3
    assert results[str(not_ready)]["result"] == "failed" and "isn't ready" in results[str(not_ready)]["detail"]
    assert (body["postedCount"], body["failedCount"], body["creditsPosted"]) == (3, 1, 9.66)
    assert len(db.scalars(select(m.Posting)).all()) == 3  # the failure undid nothing else
    assert queue(client, office) == {}


def test_batch_posting_is_safe_to_repeat(client, office, db):
    ids = [str(course_id(db, c)) for c in ("CS 2000", "ENG 110")]
    client.post("/api/postings/batch", headers=office["headers"], json={"courseIds": ids})
    again = client.post("/api/postings/batch", headers=office["headers"], json={"courseIds": ids}).json()
    assert [i["result"] for i in again["items"]] == ["already_posted", "already_posted"]
    assert again["postedCount"] == 0 and again["creditsPosted"] == 0
    assert len(db.scalars(select(m.Posting)).all()) == 2


def test_one_failing_course_does_not_stop_the_others(client, office, db):
    bad = course_id(db, "CS 2000")
    db.get(m.TranscriptCourse, bad).grade = "F"
    db.flush()
    body = client.post("/api/postings/batch", headers=office["headers"],
                       json={"courseIds": [str(bad), str(course_id(db, "ENG 110"))]}).json()
    assert [i["result"] for i in body["items"]] == ["failed", "posted"]
    assert "below the C minimum" in body["items"][0]["detail"]


@pytest.mark.parametrize("ids", [[], [str(uuid.uuid4())] * 0, [str(uuid.uuid4()) for _ in range(101)], ["not-a-uuid"]])
def test_batch_input_is_validated(client, office, ids):
    assert client.post("/api/postings/batch", headers=office["headers"], json={"courseIds": ids}).status_code == 422


# ---------------------------------------------------------------- history
def test_history_lists_what_was_posted_newest_first(client, office, db):
    for code in ("CS 2000", "ENG 110"):
        post(client, office, course_id(db, code))
    rows = client.get("/api/postings", headers=office["headers"]).json()
    assert [r["courseCode"] for r in rows] == ["ENG 110", "CS 2000"]
    assert rows[0] | {"postingId": None, "courseId": None, "postedAt": None} == {
        "postingId": None, "courseId": None, "studentRef": "STU-0001", "courseCode": "ENG 110", "result": "Elective credit",
        "creditsPosted": 3.0, "gradePosted": "T", "postedAt": None, "postedBy": "Credit Office Staff"}
    assert client.get("/api/postings?limit=1", headers=office["headers"]).json().__len__() == 1
    assert client.get("/api/postings?limit=0", headers=office["headers"]).status_code == 422
