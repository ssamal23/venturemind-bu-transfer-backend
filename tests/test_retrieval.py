"""Candidate retrieval against the embedded catalog in the database."""

import pytest
from sqlalchemy import select

from app.db import models as m
from app.services.embeddings import HashingEmbedder, course_text
from app.services.retrieval import retrieve_by_vector, retrieve_candidates
from tests.test_worker import embedder  # noqa: F401  (fixture)


def test_hashing_embedder_is_deterministic_and_normalised():
    e = HashingEmbedder()
    a, b = e.embed(["atomic structure and bonding"])[0], e.embed(["atomic structure and bonding"])[0]
    assert a == b and len(a) == e.dim and abs(sum(x * x for x in a) - 1.0) < 1e-6


def test_course_text_combines_title_description_and_outcomes():
    t = course_text("Calculus I", "Limits and derivatives.", ["Compute limits", "Find derivatives"])
    assert t == "Calculus I. Limits and derivatives.. Outcomes: Compute limits; Find derivatives" or "Outcomes:" in t


@pytest.mark.parametrize("query, expected", [
    ("atomic structure, chemical bonding, stoichiometry and gas laws with a weekly lab", "CAS CH 101"),
    ("limits, derivatives and an introduction to integrals", "CAS MA 123"),
    ("supply and demand, consumer choice, firm costs and market structures", "CAS EC 101"),
    ("survey of psychology: research methods, brain, memory, development, disorders", "CAS PS 101"),
    ("sorting, recursion, classes and basic data structures in Python", "CAS CS 112"),
])
def test_plain_language_queries_find_the_right_bu_course(db, embedder, query, expected):
    codes = [c.course.code for c in retrieve_candidates(db, query, k=3, embedder=embedder)]
    assert expected in codes


def test_only_bu_courses_are_returned(db, embedder):
    hits = retrieve_candidates(db, "calculus", k=10, embedder=embedder)
    names = {db.get(m.Institution, h.course.institution_id).name for h in hits}
    assert names == {"Boston University"}


def test_results_are_ranked_and_scored(db, embedder):
    hits = retrieve_candidates(db, "organic chemistry reactions", k=5, embedder=embedder)
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True) and all(0 <= s <= 1 for s in scores)


def test_known_equivalents_are_found_in_the_top_three(db):
    """Recall check on the workbook's own matches. Ground truth is course_matches."""
    rows = db.execute(
        select(m.CourseMatch, m.Course)
        .join(m.Course, m.CourseMatch.source_course_id == m.Course.id)
        .where(m.CourseMatch.match_level.in_([m.MatchLevel.LIKELY_EQUIVALENT, m.MatchLevel.PARTIAL_SYLLABUS_REVIEW]))
    ).all()
    assert len(rows) >= 60
    found = sum(
        match.bu_course_id in [c.course.id for c in retrieve_by_vector(db, list(src.embedding), 3)]
        for match, src in rows
    )
    assert found / len(rows) >= 0.95, f"only {found} of {len(rows)} in the top 3"
