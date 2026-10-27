import json
from decimal import Decimal

import pytest

from app.llm.matching import CandidateInfo, make_validator
from app.llm.providers.base import InvalidOutputError

SYLLABUS = (
    "General Chemistry I. Topics: atomic structure, chemical bonding, stoichiometry, gas laws and "
    "thermochemistry. Weekly lab. 4 credits."
)
CANDIDATES = [
    CandidateInfo(code="CAS CH 101", title="General Chemistry 1", credits=Decimal("4"),
                  description="Atomic structure, bonding, stoichiometry, gases, thermochemistry.",
                  outcomes=["Describe atomic structure", "Explain chemical bonding"]),
    CandidateInfo(code="CAS CH 102", title="General Chemistry 2", credits=Decimal("4"), description="Equilibrium and kinetics."),
]
GOOD = {"outcome": "BU_EQUIVALENT", "bu_course_code": "CAS CH 101", "confidence": 0.86,
        "evidence": ["Syllabus covers atomic structure, bonding and stoichiometry", "Both courses are 4 credits"],
        "gaps": "No kinetics"}


def check(payload, text=SYLLABUS):
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return make_validator(CANDIDATES, text, course_credits=Decimal("4"))(body)


def test_a_good_answer_passes():
    s = check(GOOD)
    assert (s.outcome, s.bu_course_code, s.confidence) == ("BU_EQUIVALENT", "CAS CH 101", 0.86)


def test_json_inside_a_code_fence_or_prose_is_accepted():
    assert check("```json\n" + json.dumps(GOOD) + "\n```").bu_course_code == "CAS CH 101"
    assert check("Here is my answer: " + json.dumps(GOOD) + " Thanks!").outcome == "BU_EQUIVALENT"


def test_course_code_is_normalised_but_must_be_a_candidate():
    assert check({**GOOD, "bu_course_code": "cas ch 101"}).bu_course_code == "CAS CH 101"


@pytest.mark.parametrize("code", ["CAS CH 999", "CAS CS 111", "", None, "CH 101"])
def test_a_course_that_was_not_offered_is_rejected(code):
    with pytest.raises(InvalidOutputError, match="not in the candidate list"):
        check({**GOOD, "bu_course_code": code})


def test_even_a_real_bu_course_is_rejected_if_it_was_not_a_candidate():
    with pytest.raises(InvalidOutputError):
        check({**GOOD, "bu_course_code": "CAS CS 111"})  # exists at BU, but nobody offered it


@pytest.mark.parametrize("outcome", ["ELECTIVE", "NO_CREDIT"])
def test_elective_and_no_credit_carry_no_course(outcome):
    assert check({**GOOD, "outcome": outcome, "bu_course_code": "CAS CH 101"}).bu_course_code is None


@pytest.mark.parametrize("bad", [-0.1, 1.01, "high", None])
def test_confidence_must_be_a_number_between_0_and_1(bad):
    with pytest.raises(InvalidOutputError, match="schema"):
        check({**GOOD, "confidence": bad})


@pytest.mark.parametrize("payload", ["not json at all", "{broken", "[]", '{"outcome": "APPROVE"}', ""])
def test_malformed_answers_are_rejected(payload):
    with pytest.raises(InvalidOutputError):
        check(payload)


def test_unknown_outcomes_are_rejected():
    with pytest.raises(InvalidOutputError):
        check({**GOOD, "outcome": "APPROVE_ALL"})


def test_no_evidence_is_rejected():
    with pytest.raises(InvalidOutputError, match="no evidence"):
        check({**GOOD, "evidence": []})
    with pytest.raises(InvalidOutputError, match="no evidence"):
        check({**GOOD, "evidence": ["   "]})


def test_evidence_must_be_tied_to_the_syllabus_or_the_candidates():
    with pytest.raises(InvalidOutputError, match="not tied"):
        check({**GOOD, "evidence": ["The student seems motivated and hardworking overall"]})


def test_one_grounded_item_is_enough():
    ok = check({**GOOD, "evidence": ["Vibes are great", "Covers atomic structure and bonding"]})
    assert ok.outcome == "BU_EQUIVALENT"


def test_evidence_is_trimmed_and_capped():
    s = check({**GOOD, "evidence": ["atomic structure bonding " + "x" * 600]})
    assert len(s.evidence[0]) <= 400
