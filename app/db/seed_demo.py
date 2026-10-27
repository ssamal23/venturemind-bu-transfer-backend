"""Synthetic demo data: Alex's six-course transfer scenario plus three other students' review cases.

    python -m app.db.seed_demo            # add the demo data (skips if Alex already has an application)
    python -m app.db.seed_demo --reset    # wipe transfer data first, then add it again

Run `python -m app.db.seed` first (reference data and demo users).
All people, courses and syllabi here are invented. No real student data.

--reset uses TRUNCATE, which skips the row triggers that normally block edits to
audit_logs and evaluation_decisions. It only touches transfer data, never the
catalog, matches or users.
"""

from __future__ import annotations

import argparse
import hashlib
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.db import models as m
from app.db.session import SessionLocal
from app.rules.credit_conversion import convert_credits
from app.rules.engine import RULES_VERSION, evaluate_course
from app.rules.types import CourseFacts
from app.workflows.checkpointer import close as close_checkpointer, get_checkpointer
from app.workflows.graph import park_at_review

NOW = datetime.now(timezone.utc)
TRANSFER_TABLES = [
    "audit_logs", "evaluation_decisions", "postings", "llm_calls", "jobs", "status_history",
    "evaluations", "syllabi", "transcript_courses", "transcripts", "documents", "transfer_applications",
]

BU_OUTCOMES = {
    "CAS CH 101": ["Atomic theory", "Bonding", "Stoichiometry", "Thermodynamics"],
    "CAS CH 102": ["Equilibrium", "Acids and bases", "Electrochemistry", "Kinetics"],
    "CAS CS 111": ["Programming fundamentals", "Control flow", "Basic data structures"],
}


def ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


class Demo:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.inst = {i.name: i for i in db.scalars(select(m.Institution))}
        self.bu = {
            c.code: c
            for c in db.scalars(select(m.Course).join(m.Institution).where(m.Institution.is_bu.is_(True)))
        }
        self.seq = 0
        self.review_ids: list = []
        self.start_date: date | None = None

    def user(self, email: str) -> m.User:
        found = self.db.scalar(select(m.User).where(m.User.email == email))
        if not found:
            raise SystemExit(f"User {email} not found. Run `python -m app.db.seed` first.")
        return found

    def student(self, ref: str) -> m.User:
        email = f"stu{ref[-4:]}@demo.edu"
        found = self.db.scalar(select(m.User).where(m.User.email == email))
        if found:
            return found
        u = m.User(email=email, name=f"Student {ref[-4:]}", role=m.Role.STUDENT, student_ref=ref)
        self.db.add(u)
        self.db.flush()
        return u

    def document(self, uploader: m.User, kind: m.DocType, label: str) -> m.Document:
        folder = "transcripts" if kind == m.DocType.TRANSCRIPT else "syllabi"
        doc = m.Document(
            doc_type=kind,
            uploaded_by=uploader.id,
            container=folder,
            blob_path=f"demo/{uuid.uuid4()}.pdf",
            original_filename=f"{label}.pdf",
            content_type="application/pdf",
            size_bytes=1024,
            sha256=hashlib.sha256(label.encode()).hexdigest(),
            scan_status=m.ScanStatus.CLEAN,
            uploaded_at=NOW,
        )
        self.db.add(doc)
        self.db.flush()
        return doc

    def application(self, student: m.User, days_in: int) -> m.TransferApplication:
        start = date.today() - timedelta(days=days_in)
        self.start_date = start
        app = m.TransferApplication(
            student_id=student.id,
            status=m.ApplicationStatus.IN_PROGRESS,
            bu_start_date=start,
            deadline_on=start + timedelta(days=365),
            submitted_at=ago(24),
        )
        self.db.add(app)
        self.db.flush()
        return app

    def transcript(self, app: m.TransferApplication, student: m.User, school: str) -> m.Transcript:
        doc = self.document(student, m.DocType.TRANSCRIPT, f"transcript-{school.split()[0].lower()}")
        t = m.Transcript(
            application_id=app.id,
            institution_id=self.inst[school].id,
            document_id=doc.id,
            parse_status=m.ParseStatus.PARSED,
            parsed_at=ago(24),
        )
        self.db.add(t)
        self.db.flush()
        return t

    def course(
        self,
        transcript: m.Transcript,
        *,
        code: str,
        title: str,
        grade: str,
        raw: str,
        status: m.CourseStatus,
        history: list[tuple[float, m.CourseStatus | None, m.CourseStatus, m.ActorType, str]],
        quarter: bool = False,
        final: tuple[m.Outcome, str | None, str] | None = None,
        note: str | None = None,
        source: m.Course | None = None,
        match: m.CourseMatch | None = None,
        term: str = "Fall 2025",
    ) -> m.TranscriptCourse:
        self.seq += 1
        system = "QUARTER" if quarter else "SEMESTER"
        ev = evaluate_course(CourseFacts(grade, Decimal(raw), system), bu_start_date=self.start_date, today=date.today())
        tc = m.TranscriptCourse(
            transcript_id=transcript.id,
            source_course_id=source.id if source else None,
            course_match_id=match.id if match else None,
            source_code=code,
            source_title=title,
            term=term,
            grade=grade,
            raw_credits=Decimal(raw),
            credit_system=m.CalendarType(system),
            converted_credits=convert_credits(Decimal(raw), system) if quarter else None,
            status=status,
            rule_results=ev.to_json(),
            rules_version=RULES_VERSION,
            status_note=note,
            created_at=ago(history[0][0]) + timedelta(seconds=self.seq),
        )
        if final:
            outcome, bu_code, credits = final
            tc.final_outcome = outcome
            tc.final_bu_course_id = self.bu[bu_code].id if bu_code else None
            tc.final_credits = Decimal(credits)
        self.db.add(tc)
        self.db.flush()
        for days, frm, to, actor, message in history:
            self.db.add(
                m.StatusHistory(
                    transcript_course_id=tc.id,
                    from_status=frm,
                    to_status=to,
                    actor_type=actor,
                    message=message,
                    created_at=ago(days),
                )
            )
        return tc

    def review_case(
        self,
        transcript: m.Transcript,
        student: m.User,
        *,
        code: str,
        title: str,
        grade: str,
        raw: str,
        waiting_days: float,
        syllabus_file: str,
        outcomes: list[str],
        notes: str,
        ai: dict[str, Any] | None,
        department: str,
    ) -> m.TranscriptCourse:
        S = m.CourseStatus
        tc = self.course(
            transcript,
            code=code,
            title=title,
            grade=grade,
            raw=raw,
            status=S.IN_REVIEW,
            history=[
                (24, None, S.PARSED, m.ActorType.SYSTEM, "Transcript received"),
                (24, S.PARSED, S.AWAITING_SYLLABUS, m.ActorType.SYSTEM, "No existing match: syllabus needed"),
                (waiting_days + 2, S.AWAITING_SYLLABUS, S.AI_PENDING, m.ActorType.STUDENT, f"Syllabus uploaded ({syllabus_file})"),
                (waiting_days, S.AI_PENDING, S.IN_REVIEW, m.ActorType.AI, f"Sent to the {department} department for review"),
            ],
        )
        doc = self.document(student, m.DocType.SYLLABUS, syllabus_file.removesuffix(".pdf"))
        syllabus = m.Syllabus(
            transcript_course_id=tc.id,
            document_id=doc.id,
            version=1,
            extracted_text=notes,
            learning_outcomes=outcomes,
        )
        self.db.add(syllabus)
        self.db.flush()
        ev = m.Evaluation(
            transcript_course_id=tc.id,
            syllabus_id=syllabus.id,
            created_at=ago(waiting_days),
            **(
                {
                    "status": m.EvaluationStatus.COMPLETED,
                    "provider": ai["provider"],
                    "model": ai["model"],
                    "suggested_outcome": m.Outcome(ai["outcome"]),
                    "suggested_bu_course_id": self.bu[ai["bu"]].id if ai.get("bu") else None,
                    "confidence": Decimal(str(ai["confidence"])),
                    "evidence": ai["evidence"],
                    "gaps": ai["gaps"],
                    "candidates": [{"code": ai["bu"], "score": ai["confidence"]}] if ai.get("bu") else [],
                    "prompt_version": "demo-1",
                }
                if ai
                else {"status": m.EvaluationStatus.NO_SUGGESTION}
            ),
        )
        self.db.add(ev)
        self.db.flush()
        self.review_ids.append(tc.id)  # parked at the evaluator pause once the data is committed
        return tc


def reset(db: Session) -> None:
    db.execute(text("TRUNCATE " + ", ".join(TRANSFER_TABLES) + " RESTART IDENTITY CASCADE"))
    # Saved workflows belong to the courses just deleted. get_checkpointer() makes sure the tables exist.
    get_checkpointer()
    db.execute(text("TRUNCATE checkpoints, checkpoint_blobs, checkpoint_writes"))


def seed() -> None:
    S, A = m.CourseStatus, m.ActorType
    with SessionLocal() as db:
        d = Demo(db)
        alex, rivera = d.user("student@demo.edu"), d.user("evaluator@demo.edu")
        if db.scalar(select(m.TransferApplication).where(m.TransferApplication.student_id == alex.id)):
            print("Demo data already present. Use --reset to rebuild it.")
            return

        for code, outcomes in BU_OUTCOMES.items():
            if code in d.bu and not d.bu[code].learning_outcomes:
                d.bu[code].learning_outcomes = outcomes

        # ---- Alex: six courses, four schools, 212 days left
        app = d.application(alex, days_in=153)
        nu_cs2000 = db.scalar(
            select(m.Course).join(m.Institution).where(m.Institution.name == "Northeastern University", m.Course.code == "CS 2000")
        )
        nu_match = db.scalar(select(m.CourseMatch).where(m.CourseMatch.source_course_id == nu_cs2000.id)) if nu_cs2000 else None

        t_nu = d.transcript(app, alex, "Northeastern University")
        d.course(
            t_nu, code="CS 2000", title="Introduction to Program Design and Implementation", grade="A", raw="5",
            status=S.MATCHED, source=nu_cs2000, match=nu_match,
            final=(m.Outcome.BU_EQUIVALENT, "CAS CS 111", "4"),
            note="Existing equivalency applied. Northeastern bundles a lab; BU awards 4 credits.",
            history=[(24, None, S.PARSED, A.SYSTEM, "Transcript received"), (24, S.PARSED, S.MATCHED, A.SYSTEM, "Existing equivalency found")],
        )

        t_lv = d.transcript(app, alex, "Lakeview College")
        d.course(
            t_lv, code="ECON 201", title="Principles of Microeconomics", grade="C", raw="4", quarter=True,
            status=S.MATCHED, final=(m.Outcome.BU_EQUIVALENT, "CAS EC 101", "2.66"),
            note="4 quarter credits converted at two-thirds.", term="Winter 2025",
            history=[
                (24, None, S.PARSED, A.SYSTEM, "Transcript received"),
                (24, S.PARSED, S.PARSED, A.SYSTEM, "Quarter credits converted: 4 to 2.66"),
                (24, S.PARSED, S.MATCHED, A.SYSTEM, "Existing equivalency found"),
            ],
        )

        t_nyu = d.transcript(app, alex, "New York University")
        eng = d.course(
            t_nyu, code="ENG 110", title="College Writing", grade="B", raw="3",
            status=S.DECIDED, final=(m.Outcome.ELECTIVE, None, "3"),
            history=[
                (24, None, S.PARSED, A.SYSTEM, "Transcript received"),
                (24, S.PARSED, S.AWAITING_SYLLABUS, A.SYSTEM, "No existing match: syllabus needed"),
                (18, S.AWAITING_SYLLABUS, S.AI_PENDING, A.STUDENT, "Syllabus uploaded (eng110-syllabus.pdf)"),
                (16, S.AI_PENDING, S.IN_REVIEW, A.AI, "Sent to the English department for review"),
                (15, S.IN_REVIEW, S.DECIDED, A.EVALUATOR, "Decision: elective credit"),
            ],
        )
        db.add(m.EvaluationDecision(
            transcript_course_id=eng.id, evaluator_id=rivera.id, outcome=m.DecisionOutcome.ELECTIVE,
            credits_awarded=Decimal("3"), reason="Syllabus reviewed. No single BU course fits, so elective credit is awarded.",
            decided_at=ago(15),
        ))
        d.course(
            t_nyu, code="BIO 150", title="Introduction to Biology", grade="D", raw="4",
            status=S.NOT_ELIGIBLE, final=(m.Outcome.NO_CREDIT, None, "0"),
            note="Grade D is below the C minimum most BU schools require.",
            history=[(24, None, S.PARSED, A.SYSTEM, "Transcript received"), (24, S.PARSED, S.NOT_ELIGIBLE, A.SYSTEM, "Grade below minimum: not eligible")],
        )

        t_cc = d.transcript(app, alex, "Community College X")
        d.review_case(
            t_cc, alex, code="CHEM 110", title="General Chemistry I", grade="B+", raw="4", waiting_days=2,
            syllabus_file="chem110-syllabus.pdf", department="Chemistry",
            outcomes=["Atomic structure", "Bonding", "Stoichiometry", "Thermochemistry"],
            notes="Weeks 1-6 cover gas laws and equilibrium. Lab: 2 hours weekly.",
            ai={"provider": "claude", "model": "Claude Sonnet 5.5", "outcome": "BU_EQUIVALENT", "bu": "CAS CH 101",
                "confidence": 0.86, "evidence": ["Syllabus weeks 1-6 match CH 101 core topics.", "Credit hours are equal (4 and 4)."],
                "gaps": "No mention of kinetics. Reviewer should confirm."},
        )
        d.course(
            t_cc, code="PSY 200", title="Introduction to Psychology", grade="A-", raw="3", status=S.AWAITING_SYLLABUS,
            note="Upload the course syllabus to continue.",
            history=[(24, None, S.PARSED, A.SYSTEM, "Transcript received"), (24, S.PARSED, S.AWAITING_SYLLABUS, A.SYSTEM, "No existing match: syllabus needed")],
        )

        # ---- three other students waiting in the queue
        others = [
            ("STU-3307", "Community College X", "CHEM 112", "General Chemistry II", "B", "4", 2, "chem112-syllabus.pdf", "Chemistry",
             ["Equilibrium", "Acids and bases", "Electrochemistry"], "Lab-heavy course. No organic content.",
             {"provider": "gemini", "model": "Gemini Flash", "outcome": "BU_EQUIVALENT", "bu": "CAS CH 102", "confidence": 0.64,
              "evidence": ["Three of four outcomes overlap with CH 102."], "gaps": "Kinetics is missing from the syllabus."}),
            ("STU-5190", "Northeastern University", "BIO 201", "Cell Biology", "A-", "4", 5, "bio201-syllabus.pdf", "Biology",
             ["Cell structure", "Signalling"], "Introductory level.",
             {"provider": "claude", "model": "Claude Sonnet 5.5", "outcome": "ELECTIVE", "bu": None, "confidence": 0.48,
              "evidence": ["Partial overlap with several BU biology courses."], "gaps": "No single BU course fits."}),
            ("STU-2764", "Northeastern University", "CS 2100", "Program Design and Implementation 1", "A", "5", 1, "cs2100-syllabus.pdf", "Computer Science",
             ["Program design", "Recursion", "Data structures"], "Includes a lab section. Data structures are in a separate course.", None),
        ]
        for ref, school, code, title, grade, raw, wait, sfile, dept, outcomes, notes, ai in others:
            st = d.student(ref)
            a = d.application(st, days_in=100)
            t = d.transcript(a, st, school)
            d.review_case(t, st, code=code, title=title, grade=grade, raw=raw, waiting_days=wait,
                          syllabus_file=sfile, outcomes=outcomes, notes=notes, ai=ai, department=dept)

        db.commit()
        for course_id in d.review_ids:  # each waiting course starts paused at the evaluator step
            park_at_review(course_id)
        print("Demo data added: Alex (6 courses) and 3 more students in the review queue, each paused at human review.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reset", action="store_true", help="wipe transfer data first")
    args = ap.parse_args()
    try:
        if args.reset:
            with SessionLocal() as db:
                reset(db)
                db.commit()
            print("Transfer data wiped.")
        seed()
    finally:
        close_checkpointer()


if __name__ == "__main__":
    main()
