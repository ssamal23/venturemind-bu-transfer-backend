"""Turns a parsed transcript into course rows, runs the rules, and applies existing equivalencies.

For each course:
  1. create the row (PARSED) with the credit system of the school
  2. run the course rules (grade, quarter conversion, one-year window)
  3. a failed rule makes it NOT_ELIGIBLE, with the reason
  4. otherwise, if the equivalency database holds a "likely equivalent" BU course and that
     course passes the program rules, apply it (MATCHED)
  5. otherwise it needs a syllabus (AWAITING_SYLLABUS) for the AI and a department to review

Nothing here uses an AI to decide anything. The only model call is the reading of the
transcript, and every course it returns has already been checked against the text.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import write_audit
from app.db import models as m
from app.llm.transcripts import ParsedTranscript
from app.rules.credit_conversion import credits_for_bu_equivalent
from app.rules.engine import RULES_VERSION
from app.services.rules_service import evaluate_proposed_equivalent, evaluate_stored_course
from app.services.status import change_status


def _squash(code: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", code).upper()


def apply_parsed_transcript(db: Session, transcript: m.Transcript, parsed: ParsedTranscript, today: date | None = None) -> dict[str, int]:
    today = today or date.today()
    institution = db.get(m.Institution, transcript.institution_id)
    credit_system = institution.calendar_type  # the school's calendar decides conversion, not the model

    catalog = {_squash(c.code): c for c in db.scalars(select(m.Course).where(m.Course.institution_id == institution.id))}
    bu_codes = {c.id: c for c in db.scalars(select(m.Course).join(m.Institution).where(m.Institution.is_bu.is_(True)))}
    application = db.get(m.TransferApplication, transcript.application_id)

    counts = {"matched": 0, "not_eligible": 0, "awaiting_syllabus": 0}
    for line in parsed.courses:
        source = catalog.get(_squash(line.code))
        course = m.TranscriptCourse(
            transcript_id=transcript.id,
            source_course_id=source.id if source else None,
            source_code=line.code,
            source_title=line.title or (source.title if source else None),
            term=line.term,
            grade=line.grade,
            raw_credits=line.credits,
            credit_system=credit_system,
            status=m.CourseStatus.PARSED,
            created_at=datetime.now(timezone.utc),
        )
        db.add(course)
        db.flush()
        db.add(m.StatusHistory(
            transcript_course_id=course.id, from_status=None, to_status=m.CourseStatus.PARSED,
            actor_type=m.ActorType.SYSTEM, message="Course read from your transcript",
            created_at=datetime.now(timezone.utc),
        ))

        evaluation = evaluate_stored_course(db, course, today)
        course.rule_results = evaluation.to_json()
        course.rules_version = RULES_VERSION
        if credit_system == m.CalendarType.QUARTER:
            course.converted_credits = evaluation.converted_credits
            db.add(m.StatusHistory(
                transcript_course_id=course.id, from_status=m.CourseStatus.PARSED, to_status=m.CourseStatus.PARSED,
                actor_type=m.ActorType.SYSTEM, created_at=datetime.now(timezone.utc),
                message=f"Quarter credits converted: {line.credits.normalize():f} to {evaluation.converted_credits}",
            ))

        system = dict(actor_type=m.ActorType.SYSTEM, actor_id=None)
        if not evaluation.eligible:
            course.final_outcome = m.Outcome.NO_CREDIT
            course.final_credits = 0
            reasons = "; ".join(c.label for c in evaluation.blocking)
            change_status(db, course, m.CourseStatus.NOT_ELIGIBLE, message=f"Not eligible: {reasons}"[:300], note=reasons, **system)
            counts["not_eligible"] += 1
            continue

        match = _applicable_match(db, source)
        if match is not None:
            bu = bu_codes[match.bu_course_id]
            target = evaluate_proposed_equivalent(db, course, bu)
            if target.eligible:
                course.course_match_id = match.id
                course.final_outcome = m.Outcome.BU_EQUIVALENT
                course.final_bu_course_id = bu.id
                course.final_credits = credits_for_bu_equivalent(credit_system.value, course.converted_credits, course.raw_credits, bu.credits)
                course.rule_results = course.rule_results + target.to_json()
                change_status(db, course, m.CourseStatus.MATCHED, message="Existing equivalency found",
                              note="Existing equivalency applied.", **system)
                counts["matched"] += 1
                continue

        change_status(db, course, m.CourseStatus.AWAITING_SYLLABUS, message="No existing match: syllabus needed",
                      note="Upload the course syllabus to continue.", **system)
        counts["awaiting_syllabus"] += 1

    transcript.parse_status = m.ParseStatus.PARSED
    transcript.parse_error = None
    transcript.parsed_at = datetime.now(timezone.utc)
    if application.status in (m.ApplicationStatus.DRAFT, m.ApplicationStatus.SUBMITTED):
        application.status = m.ApplicationStatus.IN_PROGRESS
        application.submitted_at = application.submitted_at or datetime.now(timezone.utc)
    write_audit(db, actor_type=m.ActorType.SYSTEM, actor_id=None, action="transcript.parsed",
                entity_type="transcript", entity_id=transcript.id, after={"courses": len(parsed.courses), **counts})
    return counts


def _applicable_match(db: Session, source: m.Course | None) -> m.CourseMatch | None:
    """Only a 'likely equivalent' match is applied on its own. Partial matches and
    'must be taken at BU' courses go to a syllabus review instead."""
    if source is None:
        return None
    return db.scalar(
        select(m.CourseMatch).where(
            m.CourseMatch.source_course_id == source.id,
            m.CourseMatch.bu_course_id.is_not(None),
            m.CourseMatch.match_level == m.MatchLevel.LIKELY_EQUIVALENT,
        ).limit(1)
    )
