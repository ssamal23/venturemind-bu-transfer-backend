"""The evaluation worker, end to end, with fake LLM providers and the real embedding model."""

import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

import pytest
from sqlalchemy import select

from app.config import settings
from app.db import models as m
from app.llm.gateway import LLMGateway
from app.llm.providers.testing import FailingProvider, FakeProvider
from app.services.embeddings import FastEmbedder
from app.workers.queue import claim_job
from app.workers.evaluation_worker import run_once

PSY_SYLLABUS = (
    "PSY 200 Introduction to Psychology. This course surveys the science of mind and behavior. "
    "Topics include research methods, the brain and behavior, learning, memory, human development, "
    "social behavior, and psychological disorders. Four credit hours. Weekly readings and two exams."
)
GOOD_ANSWER = {
    "outcome": "BU_EQUIVALENT", "bu_course_code": "CAS PS 101", "confidence": 0.9,
    "evidence": ["Syllabus covers research methods, the brain and behavior, learning and memory",
                 "Surveys social behavior and psychological disorders like General Psychology"],
    "gaps": "None found",
}


def make_docx(text: str, extra_files: dict[str, bytes] | None = None) -> bytes:
    paras = "".join(f"<w:p><w:r><w:t>{escape(line)}</w:t></w:r></w:p>" for line in text.splitlines())
    xml = ('<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f"<w:body>{paras}</w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
        z.writestr("[Content_Types].xml", "<Types/>")
        for name, data in (extra_files or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


@pytest.fixture(scope="session")
def embedder():
    return FastEmbedder()


def gateway_factory(*providers, **kw):
    def factory(on_call=None):
        return LLMGateway(list(providers), sleep=lambda s: None, jitter=lambda: 0.0, on_call=on_call, **kw)
    return factory


@pytest.fixture()
def psy_course(client, student, db):
    """Alex's PSY 200, waiting for a syllabus."""
    data = client.get(f"/api/students/{student['user']['id']}/dashboard", headers=student["headers"]).json()
    return next(c for c in data["courses"] if c["sourceCode"] == "PSY 200")["id"]


def upload(client, student, course_id, content: bytes, name="psy200.docx"):
    res = client.post(f"/api/transcript-courses/{course_id}/syllabus", headers=student["headers"],
                      files={"file": (name, content, "application/octet-stream")})
    assert res.status_code == 201, res.text
    return res


def course_row(db, course_id):
    return db.scalar(select(m.TranscriptCourse).where(m.TranscriptCourse.id == course_id))


def job_row(db, course_id):
    return db.scalar(select(m.Job).where(m.Job.transcript_course_id == course_id))


# ---------------------------------------------------------------- happy path
def test_upload_becomes_an_ai_suggestion_waiting_for_a_human(client, student, evaluator, db, psy_course, embedder):
    upload(client, student, psy_course, make_docx(PSY_SYLLABUS))
    fake = FakeProvider("claude", "claude-sonnet-5-5", json.dumps(GOOD_ANSWER))

    assert run_once(db, gateway_factory=gateway_factory(fake), embedder=embedder) == "suggested"

    course = course_row(db, psy_course)
    assert course.status == m.CourseStatus.IN_REVIEW
    assert course.final_outcome is None and course.final_credits is None  # the AI decided nothing
    assert db.scalar(select(m.EvaluationDecision).where(m.EvaluationDecision.transcript_course_id == psy_course)) is None

    ev = db.scalar(select(m.Evaluation).where(m.Evaluation.transcript_course_id == psy_course))
    assert ev.status == m.EvaluationStatus.COMPLETED and ev.provider == "claude" and float(ev.confidence) == 0.9
    assert ev.suggested_outcome == m.Outcome.BU_EQUIVALENT and ev.prompt_version == "match-1"
    assert any(c["code"] == "CAS PS 101" for c in ev.candidates)

    call = db.scalar(select(m.LlmCall).where(m.LlmCall.evaluation_id == ev.id))
    assert call.success and not call.fallback_used and call.input_tokens == 100
    assert job_row(db, psy_course).status == m.JobStatus.SUCCEEDED

    syllabus = db.scalar(select(m.Syllabus).where(m.Syllabus.transcript_course_id == psy_course))
    assert "research methods" in syllabus.extracted_text and syllabus.embedding is not None
    assert db.get(m.Document, syllabus.document_id).scan_status == m.ScanStatus.CLEAN

    # visible to the evaluator, with the right model label, and in the student's timeline
    queue = client.get("/api/review-queue", headers=evaluator["headers"]).json()
    case = next(c for c in queue if c["courseCode"] == "PSY 200")
    assert case["department"] == "Psychology" and case["ai"]["model"] == "Claude Sonnet 5.5"
    assert case["buCourse"]["code"] == "CAS PS 101"
    dash = client.get(f"/api/students/{student['user']['id']}/dashboard", headers=student["headers"]).json()
    psy = next(c for c in dash["courses"] if c["sourceCode"] == "PSY 200")
    assert psy["timeline"][-1]["label"] == "Sent to the Psychology department for review"


def test_the_evaluator_can_then_decide_and_the_rules_still_apply(client, student, evaluator, db, psy_course, embedder):
    upload(client, student, psy_course, make_docx(PSY_SYLLABUS))
    run_once(db, gateway_factory=gateway_factory(FakeProvider("claude", reply=json.dumps(GOOD_ANSWER))), embedder=embedder)
    res = client.post(f"/api/transcript-courses/{psy_course}/decision", headers=evaluator["headers"],
                      json={"outcome": "BU_EQUIVALENT", "reason": "Matches PS 101."})
    assert res.status_code == 200 and res.json()["creditsAwarded"] == 4


def test_an_empty_queue_returns_none(db):
    # Other tests' jobs are rolled back; drain anything the seed left.
    while claim_job(db, "drain") is not None:
        pass
    assert run_once(db) is None


# ---------------------------------------------------------------- failover and outages
def test_claude_down_falls_back_to_gemini_and_says_so(client, student, evaluator, db, psy_course, embedder):
    upload(client, student, psy_course, make_docx(PSY_SYLLABUS))
    claude = FailingProvider("claude", "claude-sonnet-5-5", kind="rate_limit", retryable=True)
    gemini = FakeProvider("gemini", "gemini-2.5-flash", json.dumps(GOOD_ANSWER))

    assert run_once(db, gateway_factory=gateway_factory(claude, gemini), embedder=embedder) == "suggested"

    ev = db.scalar(select(m.Evaluation).where(m.Evaluation.transcript_course_id == psy_course))
    assert ev.provider == "gemini"
    calls = db.scalars(select(m.LlmCall).where(m.LlmCall.evaluation_id == ev.id).order_by(m.LlmCall.created_at)).all()
    assert [(c.provider, c.success, c.fallback_used) for c in calls][-1] == ("gemini", True, True)
    assert any(c.provider == "claude" and not c.success and c.error_type == "rate_limit" for c in calls)

    case = next(c for c in client.get("/api/review-queue", headers=evaluator["headers"]).json() if c["courseCode"] == "PSY 200")
    assert case["ai"]["model"] == "Gemini Flash (fallback)"


def test_every_provider_down_requeues_and_never_decides(client, student, db, psy_course, embedder, monkeypatch):
    monkeypatch.setattr(settings, "worker_max_attempts", 3)
    upload(client, student, psy_course, make_docx(PSY_SYLLABUS))
    down = gateway_factory(FailingProvider("claude"), FailingProvider("gemini", kind="overloaded"), max_attempts=1)

    assert run_once(db, gateway_factory=down, embedder=embedder) == "requeued"

    job = job_row(db, psy_course)
    assert job.status == m.JobStatus.QUEUED and job.attempts == 1
    assert job.next_run_at > datetime.now(timezone.utc)  # waits before the next try
    assert "All LLM providers failed" in job.last_error
    course = course_row(db, psy_course)
    assert course.status == m.CourseStatus.AI_PENDING and course.final_outcome is None  # still pending, nothing decided
    assert run_once(db, gateway_factory=down, embedder=embedder) is None  # not due yet


def test_after_the_last_retry_the_case_goes_to_a_human_with_no_suggestion(client, student, evaluator, db, psy_course, embedder, monkeypatch):
    monkeypatch.setattr(settings, "worker_max_attempts", 1)
    upload(client, student, psy_course, make_docx(PSY_SYLLABUS))
    down = gateway_factory(FailingProvider("claude"), max_attempts=1)

    assert run_once(db, gateway_factory=down, embedder=embedder) == "no_suggestion"

    assert course_row(db, psy_course).status == m.CourseStatus.IN_REVIEW
    assert course_row(db, psy_course).final_outcome is None
    assert job_row(db, psy_course).status == m.JobStatus.DEAD
    ev = db.scalar(select(m.Evaluation).where(m.Evaluation.transcript_course_id == psy_course))
    assert ev.status == m.EvaluationStatus.NO_SUGGESTION and "No AI provider" in ev.gaps
    case = next(c for c in client.get("/api/review-queue", headers=evaluator["headers"]).json() if c["courseCode"] == "PSY 200")
    assert case["ai"] is None  # the evaluator sees "no AI suggestion" and reviews manually


def test_a_crashed_worker_s_job_is_picked_up_again(client, student, db, psy_course):
    upload(client, student, psy_course, make_docx(PSY_SYLLABUS))
    first = claim_job(db, "worker-a")
    assert first is not None and first.status == m.JobStatus.RUNNING
    assert claim_job(db, "worker-b") is None  # already claimed
    first.locked_at = datetime.now(timezone.utc) - timedelta(minutes=30)  # worker-a died
    db.flush()
    again = claim_job(db, "worker-b")
    assert again is not None and again.id == first.id and again.locked_by == "worker-b" and again.attempts == 2


# ---------------------------------------------------------------- bad files
def test_unreadable_text_goes_to_a_human_without_calling_the_model(client, student, db, psy_course, embedder):
    upload(client, student, psy_course, make_docx("too short"))
    fake = FakeProvider("claude", reply=json.dumps(GOOD_ANSWER))
    assert run_once(db, gateway_factory=gateway_factory(fake), embedder=embedder) == "no_suggestion"
    assert fake.calls == []
    ev = db.scalar(select(m.Evaluation).where(m.Evaluation.transcript_course_id == psy_course))
    assert "little or no readable text" in ev.gaps


def test_a_docx_with_macros_is_blocked_before_any_reading(client, student, db, psy_course, embedder):
    upload(client, student, psy_course, make_docx(PSY_SYLLABUS, {"word/vbaProject.bin": b"x"}))
    fake = FakeProvider("claude", reply=json.dumps(GOOD_ANSWER))
    assert run_once(db, gateway_factory=gateway_factory(fake), embedder=embedder) == "no_suggestion"
    assert fake.calls == []
    syllabus = db.scalar(select(m.Syllabus).where(m.Syllabus.transcript_course_id == psy_course))
    assert db.get(m.Document, syllabus.document_id).scan_status == m.ScanStatus.REJECTED
