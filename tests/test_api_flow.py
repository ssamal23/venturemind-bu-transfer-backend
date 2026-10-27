from tests.conftest import login

PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n"


def course_by_code(courses, code):
    return next(c for c in courses if c["sourceCode"] == code)


# ---------------------------------------------------------------- auth
def test_login_returns_token_and_lowercase_role(client):
    res = client.post("/api/auth/login", json={"email": "student@demo.edu", "password": "demo1234"})
    assert res.status_code == 200
    body = res.json()
    assert body["user"]["role"] == "student" and body["token"]


def test_login_rejects_wrong_password_and_unknown_email(client):
    for email, pw in (("student@demo.edu", "wrong"), ("nobody@demo.edu", "demo1234")):
        res = client.post("/api/auth/login", json={"email": email, "password": pw})
        assert res.status_code == 401
        assert res.json()["detail"] == "Email or password is incorrect."


def test_endpoints_require_a_token(client):
    assert client.get("/api/review-queue").status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer junk"}).status_code == 401


def test_me(client, student):
    assert client.get("/api/auth/me", headers=student["headers"]).json()["email"] == "student@demo.edu"


# ---------------------------------------------------------------- student dashboard
def test_dashboard_matches_the_demo_scenario(client, student):
    res = client.get(f"/api/students/{student['user']['id']}/dashboard", headers=student["headers"])
    assert res.status_code == 200
    data = res.json()
    assert len(data["courses"]) == 6
    assert data["daysLeft"] == 212

    statuses = {c["sourceCode"]: c["status"] for c in data["courses"]}
    assert statuses == {
        "CS 2000": "MATCHED", "ECON 201": "MATCHED", "ENG 110": "DECIDED",
        "CHEM 110": "IN_REVIEW", "PSY 200": "AWAITING_SYLLABUS", "BIO 150": "NOT_ELIGIBLE",
    }
    econ = course_by_code(data["courses"], "ECON 201")
    assert econ["credits"] == 2.66  # 4 quarter credits at two-thirds
    assert course_by_code(data["courses"], "CHEM 110")["credits"] is None  # pending, not zero units
    assert course_by_code(data["courses"], "CHEM 110")["note"] == "With the Chemistry department."
    assert course_by_code(data["courses"], "ENG 110")["timeline"][-1]["label"] == "Decision: elective credit"


def test_student_cannot_read_another_students_dashboard(client, student, evaluator):
    res = client.get(f"/api/students/{evaluator['user']['id']}/dashboard", headers=student["headers"])
    assert res.status_code == 403


def test_evaluator_cannot_open_student_dashboard(client, evaluator):
    res = client.get(f"/api/students/{evaluator['user']['id']}/dashboard", headers=evaluator["headers"])
    assert res.status_code == 403


# ---------------------------------------------------------------- syllabus upload
def psy_id(client, student):
    data = client.get(f"/api/students/{student['user']['id']}/dashboard", headers=student["headers"]).json()
    return course_by_code(data["courses"], "PSY 200")["id"]


def test_syllabus_upload_moves_course_to_evaluation(client, student):
    cid = psy_id(client, student)
    res = client.post(
        f"/api/transcript-courses/{cid}/syllabus", headers=student["headers"],
        files={"file": ("psy200.pdf", PDF, "application/pdf")},
    )
    assert res.status_code == 201, res.text
    assert res.json() == {"courseId": cid, "status": "AI_PENDING", "version": 1}

    data = client.get(f"/api/students/{student['user']['id']}/dashboard", headers=student["headers"]).json()
    psy = course_by_code(data["courses"], "PSY 200")
    assert psy["status"] == "IN_REVIEW"
    assert psy["note"] == "Syllabus received. Evaluation has started."
    assert psy["timeline"][-1]["label"].startswith("Syllabus uploaded")


def test_syllabus_upload_rejects_bad_files(client, student):
    cid = psy_id(client, student)
    url = f"/api/transcript-courses/{cid}/syllabus"
    h = student["headers"]
    assert client.post(url, headers=h, files={"file": ("x.exe", PDF, "application/octet-stream")}).status_code == 415
    assert client.post(url, headers=h, files={"file": ("x.pdf", b"not a pdf", "application/pdf")}).status_code == 415
    assert client.post(url, headers=h, files={"file": ("x.pdf", b"", "application/pdf")}).status_code == 422


def test_syllabus_only_when_waiting_for_one(client, student):
    data = client.get(f"/api/students/{student['user']['id']}/dashboard", headers=student["headers"]).json()
    cid = course_by_code(data["courses"], "CS 2000")["id"]  # already matched
    res = client.post(f"/api/transcript-courses/{cid}/syllabus", headers=student["headers"],
                      files={"file": ("a.pdf", PDF, "application/pdf")})
    assert res.status_code == 409


# ---------------------------------------------------------------- evaluator queue and decisions
def test_review_queue_lists_four_cases_and_blocks_students(client, student, evaluator):
    assert client.get("/api/review-queue", headers=student["headers"]).status_code == 403
    queue = client.get("/api/review-queue", headers=evaluator["headers"]).json()
    assert [c["courseCode"] for c in queue][0] == "BIO 201"  # waiting longest
    assert len(queue) >= 4
    chem = next(c for c in queue if c["courseCode"] == "CHEM 110")
    assert chem["department"] == "Chemistry"
    assert chem["ai"]["confidence"] == 0.86 and chem["ai"]["model"] == "Claude Sonnet 5.5"
    assert chem["buCourse"]["code"] == "CAS CH 101" and chem["buCourse"]["credits"] == 4
    fallback = next(c for c in queue if c["courseCode"] == "CHEM 112")
    assert fallback["ai"]["model"] == "Gemini Flash (fallback)"
    assert next(c for c in queue if c["courseCode"] == "CS 2100")["ai"] is None
    assert all(set(r) == {"label", "pass", "note"} for r in chem["rules"])


def chem_case(client, evaluator):
    return next(c for c in client.get("/api/review-queue", headers=evaluator["headers"]).json() if c["courseCode"] == "CHEM 110")


def test_decision_requires_a_reason(client, evaluator):
    cid = chem_case(client, evaluator)["id"]
    for body in ({"outcome": "ELECTIVE", "reason": ""}, {"outcome": "ELECTIVE", "reason": "   "}):
        assert client.post(f"/api/transcript-courses/{cid}/decision", headers=evaluator["headers"], json=body).status_code == 422


def test_approval_updates_the_student_view_and_leaves_the_queue(client, student, evaluator):
    cid = chem_case(client, evaluator)["id"]
    res = client.post(f"/api/transcript-courses/{cid}/decision", headers=evaluator["headers"],
                      json={"outcome": "BU_EQUIVALENT", "reason": "Covers CH 101 core topics."})
    assert res.status_code == 200 and res.json()["courseStatus"] == "DECIDED" and res.json()["creditsAwarded"] == 4

    queue = client.get("/api/review-queue", headers=evaluator["headers"]).json()
    assert all(c["id"] != cid for c in queue)

    data = client.get(f"/api/students/{student['user']['id']}/dashboard", headers=student["headers"]).json()
    chem = course_by_code(data["courses"], "CHEM 110")
    assert (chem["status"], chem["buResult"], chem["credits"]) == ("DECIDED", "CAS CH 101", 4.0)
    assert chem["timeline"][-1]["label"] == "Decision: equivalent to CAS CH 101"

    # deciding twice is refused
    again = client.post(f"/api/transcript-courses/{cid}/decision", headers=evaluator["headers"],
                        json={"outcome": "NO_CREDIT", "reason": "changed my mind"})
    assert again.status_code == 409


def test_equivalent_needs_a_bu_course_when_ai_suggested_none(client, evaluator):
    queue = client.get("/api/review-queue", headers=evaluator["headers"]).json()
    cid = next(c for c in queue if c["courseCode"] == "CS 2100")["id"]  # no AI suggestion
    res = client.post(f"/api/transcript-courses/{cid}/decision", headers=evaluator["headers"],
                      json={"outcome": "BU_EQUIVALENT", "reason": "Looks like CS 111"})
    assert res.status_code == 422
    ok = client.post(f"/api/transcript-courses/{cid}/decision", headers=evaluator["headers"],
                     json={"outcome": "BU_EQUIVALENT", "reason": "Looks like CS 111", "buCourseCode": "CAS CS 111"})
    assert ok.status_code == 200 and ok.json()["creditsAwarded"] == 4


def test_request_more_info_sends_the_student_back_to_upload(client, student, evaluator):
    cid = chem_case(client, evaluator)["id"]
    res = client.post(f"/api/transcript-courses/{cid}/decision", headers=evaluator["headers"],
                      json={"outcome": "REQUEST_INFO", "reason": "Please add the lab syllabus."})
    assert res.status_code == 200 and res.json()["courseStatus"] == "PENDING_INFO"
    chem = course_by_code(
        client.get(f"/api/students/{student['user']['id']}/dashboard", headers=student["headers"]).json()["courses"], "CHEM 110")
    assert chem["status"] == "AWAITING_SYLLABUS" and "lab syllabus" in chem["note"]


def test_students_cannot_decide(client, student, evaluator):
    cid = chem_case(client, evaluator)["id"]
    res = client.post(f"/api/transcript-courses/{cid}/decision", headers=student["headers"],
                      json={"outcome": "BU_EQUIVALENT", "reason": "give me credit"})
    assert res.status_code == 403


def test_decisions_are_audited(client, evaluator, db):
    from sqlalchemy import select
    from app.db.models import AuditLog

    cid = chem_case(client, evaluator)["id"]
    client.post(f"/api/transcript-courses/{cid}/decision", headers=evaluator["headers"],
                json={"outcome": "NO_CREDIT", "reason": "Not equivalent."})
    actions = [a.action for a in db.scalars(select(AuditLog).order_by(AuditLog.id))]
    assert "decision.recorded" in actions and "course.status_changed" in actions


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}


# ---------------------------------------------------------------- rules enforced at decision time
def cs2100_id(client, evaluator):
    queue = client.get("/api/review-queue", headers=evaluator["headers"]).json()
    return next(c for c in queue if c["courseCode"] == "CS 2100")["id"]


def decide(client, evaluator, cid, **body):
    body.setdefault("reason", "Reviewed the syllabus.")
    return client.post(f"/api/transcript-courses/{cid}/decision", headers=evaluator["headers"], json=body)


def test_group_c_course_cannot_be_awarded_even_if_the_evaluator_asks(client, evaluator):
    cid = cs2100_id(client, evaluator)
    res = decide(client, evaluator, cid, outcome="BU_EQUIVALENT", buCourseCode="CAS CS 320")
    assert res.status_code == 409
    assert "can't be transferred" in res.json()["detail"] or "in residence" in res.json()["detail"]


def test_a_failing_grade_blocks_credit_but_not_no_credit(client, evaluator, db):
    from sqlalchemy import select
    from app.db.models import TranscriptCourse

    cid = cs2100_id(client, evaluator)
    db.scalar(select(TranscriptCourse).where(TranscriptCourse.id == cid)).grade = "D"
    db.flush()
    blocked = decide(client, evaluator, cid, outcome="BU_EQUIVALENT", buCourseCode="CAS CS 111")
    assert blocked.status_code == 409 and "below the C minimum" in blocked.json()["detail"]
    assert decide(client, evaluator, cid, outcome="ELECTIVE").status_code == 409
    assert decide(client, evaluator, cid, outcome="NO_CREDIT").status_code == 200


def test_an_expired_transfer_window_blocks_credit(client, evaluator, db):
    from datetime import date, timedelta
    from sqlalchemy import select
    from app.db.models import Transcript, TranscriptCourse, TransferApplication

    cid = cs2100_id(client, evaluator)
    app = db.scalar(
        select(TransferApplication).join(Transcript, Transcript.application_id == TransferApplication.id)
        .join(TranscriptCourse, TranscriptCourse.transcript_id == Transcript.id).where(TranscriptCourse.id == cid)
    )
    app.bu_start_date = date.today() - timedelta(days=500)
    db.flush()
    res = decide(client, evaluator, cid, outcome="ELECTIVE")
    assert res.status_code == 409 and "1-year transfer window ended" in res.json()["detail"]


def test_rules_are_rechecked_on_live_data_not_the_stored_results(client, evaluator, db):
    from sqlalchemy import select
    from app.db.models import TranscriptCourse

    cid = cs2100_id(client, evaluator)
    tc = db.scalar(select(TranscriptCourse).where(TranscriptCourse.id == cid))
    tc.grade = "F"
    tc.rule_results = [{"label": "Grade F meets the C minimum", "pass": True, "note": False}]  # stale, wrong
    db.flush()
    assert decide(client, evaluator, cid, outcome="ELECTIVE").status_code == 409


def test_approval_stores_the_rule_results_and_version(client, evaluator, db):
    from sqlalchemy import select
    from app.db.models import TranscriptCourse

    cid = cs2100_id(client, evaluator)
    assert decide(client, evaluator, cid, outcome="BU_EQUIVALENT", buCourseCode="CAS CS 111").status_code == 200
    tc = db.scalar(select(TranscriptCourse).where(TranscriptCourse.id == cid))
    assert tc.rules_version == "1.0.0"
    assert {"grade", "deadline", "credit_conversion"} <= {r["rule"] for r in tc.rule_results}
