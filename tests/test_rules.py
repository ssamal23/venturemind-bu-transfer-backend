from datetime import date
from decimal import Decimal

import pytest

from app.rules.credit_conversion import check_credits, convert_credits
from app.rules.deadline_rule import check_deadline, deadline_for
from app.rules.engine import RULES_VERSION, evaluate_course, evaluate_target
from app.rules.grade_rule import check_grade
from app.rules.program_rules import check_target, course_level
from app.rules.types import CourseFacts, ProgramRule

D = Decimal


# ---------------------------------------------------------------- credit conversion
@pytest.mark.parametrize(
    "raw, expected",
    [("4", "2.66"), ("3", "2.00"), ("5", "3.33"), ("1", "0.66"), ("4.5", "3.00"), ("0", "0.00"), ("2", "1.33")],
)
def test_quarter_credits_convert_at_two_thirds_truncated(raw, expected):
    assert convert_credits(D(raw), "QUARTER") == D(expected)


def test_bu_example_is_2_66_not_2_67():
    assert convert_credits(D("4"), "QUARTER") == D("2.66")


@pytest.mark.parametrize("raw", ["3", "4", "5", "1.5"])
def test_semester_credits_are_unchanged(raw):
    assert convert_credits(D(raw), "SEMESTER") == D(raw).quantize(D("0.01"))


def test_credit_system_is_case_insensitive():
    assert convert_credits(D("4"), "quarter") == D("2.66")


def test_credit_check_labels():
    assert check_credits(D("4"), "QUARTER").label == "Quarter credits converted: 4 to 2.66"
    assert check_credits(D("4.00"), "QUARTER").label == "Quarter credits converted: 4 to 2.66"
    assert check_credits(D("4"), "SEMESTER").label == "Semester credits, no conversion"
    assert not check_credits(D("4"), "QUARTER").blocking


# ---------------------------------------------------------------- grade
@pytest.mark.parametrize("grade", ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", " c ", "b+"])
def test_grades_at_or_above_c_pass(grade):
    c = check_grade(grade)
    assert c.passed and not c.note and not c.blocking


@pytest.mark.parametrize("grade", ["C-", "D+", "D", "D-", "F"])
def test_grades_below_c_block_credit(grade):
    c = check_grade(grade)
    assert c.blocking and "below the C minimum" in c.label


@pytest.mark.parametrize("grade", ["W", "I", "NP", "AU"])
def test_marks_that_earn_no_credit_block(grade):
    assert check_grade(grade).blocking


@pytest.mark.parametrize("grade", ["P", "S", "CR", "XYZ", "", None])
def test_unclear_grades_warn_but_do_not_block(grade):
    c = check_grade(grade)
    assert c.passed and c.note and not c.blocking  # an evaluator decides


def test_unicode_minus_is_understood():
    assert check_grade("B−").passed
    assert check_grade("C−").blocking


def test_minimum_grade_is_configurable():
    assert check_grade("C", minimum="B").blocking
    assert check_grade("B", minimum="B").passed


# ---------------------------------------------------------------- deadline
def test_deadline_is_one_year_after_start():
    assert deadline_for(date(2026, 5, 10)) == date(2027, 5, 10)


def test_leap_day_start_falls_back_to_28_feb():
    assert deadline_for(date(2024, 2, 29)) == date(2025, 2, 28)


def test_deadline_boundaries():
    start = date(2026, 1, 1)
    assert check_deadline(start, date(2027, 1, 1)).note  # last day: only 0 days left, still allowed
    assert not check_deadline(start, date(2027, 1, 1)).blocking
    late = check_deadline(start, date(2027, 1, 2))
    assert late.blocking and "ended on 2027-01-01" in late.label


def test_deadline_warns_in_last_30_days_and_is_plain_before_that():
    start = date(2026, 1, 1)
    warn = check_deadline(start, date(2026, 12, 15))
    assert warn.passed and warn.note and "17 days" in warn.label
    ok = check_deadline(start, date(2026, 6, 1))
    assert ok.passed and not ok.note and "214 days left" in ok.label
    assert "1 day left" in check_deadline(start, date(2026, 12, 31)).label


def test_unknown_start_date_does_not_block():
    c = check_deadline(None, date(2026, 6, 1))
    assert c.passed and c.note


# ---------------------------------------------------------------- program rules
GROUP_C = [ProgramRule("NON_TRANSFERABLE", course_code=c, group_label="Group C") for c in ("CAS CS 320", "CAS CS 332", "CAS CS 350")]
ECON_CAP = [ProgramRule("MAX_TRANSFER", max_count=4, min_level=200)]
BIO_RES = [ProgramRule("MUST_TAKE_AT_BU", max_count=5, params={"course_prefix": "BI", "beyond": ["BI 107", "BI 108"]})]


def test_course_level_parsing():
    assert course_level("CAS EC 201") == 201
    assert course_level("CAS CS 111") == 111
    assert course_level("NO NUMBER") is None


@pytest.mark.parametrize("code", ["CAS CS 320", "CAS CS 332", "CAS CS 350"])
def test_group_c_courses_cannot_be_transferred(code):
    checks = check_target(code, GROUP_C)
    assert len(checks) == 1 and checks[0].blocking and "Group C" in checks[0].label


def test_other_cs_courses_are_fine():
    assert check_target("CAS CS 111", GROUP_C) == []


def test_must_take_at_bu_from_the_equivalency_database_blocks():
    c = check_target("CAS CS 332", [], must_take_at_bu=True)[0]
    assert c.blocking and "in residence" in c.label


def test_economics_cap_counts_only_upper_level_transfers():
    used = ["CAS EC 201", "CAS EC 202", "CAS EC 101"]  # EC 101 is below 200 and doesn't count
    ok = check_target("CAS EC 203", ECON_CAP, used)[0]
    assert ok.passed and ok.note and "3 of 4" in ok.label


def test_economics_cap_blocks_the_fifth():
    used = ["CAS EC 201", "CAS EC 202", "CAS EC 203", "CAS EC 204"]
    c = check_target("CAS EC 301", ECON_CAP, used)[0]
    assert c.blocking and "limit of 4" in c.label


def test_economics_cap_ignores_lower_level_courses():
    used = ["CAS EC 201", "CAS EC 202", "CAS EC 203", "CAS EC 204"]
    assert check_target("CAS EC 101", ECON_CAP, used) == []


def test_recrediting_the_same_course_is_not_counted_twice():
    used = ["CAS EC 201", "CAS EC 202", "CAS EC 203", "CAS EC 204"]
    assert not check_target("CAS EC 204", ECON_CAP, used)[0].blocking


def test_biology_residency_note_applies_beyond_107_108():
    note = check_target("CAS BI 203", BIO_RES)[0]
    assert note.passed and note.note and "5 BI courses" in note.label
    assert check_target("CAS BI 107", BIO_RES) == []
    assert check_target("CAS CH 101", BIO_RES) == []


# ---------------------------------------------------------------- engine
def facts(grade="B", raw="4", system="SEMESTER"):
    return CourseFacts(grade=grade, raw_credits=D(raw), credit_system=system)


def test_engine_returns_all_course_checks_and_converted_credits():
    ev = evaluate_course(facts("C", "4", "QUARTER"), bu_start_date=date(2026, 5, 1), today=date(2026, 10, 1))
    assert ev.converted_credits == D("2.66")
    assert [c.rule for c in ev.checks] == ["grade", "credit_conversion", "deadline", "hub", "gpa"]
    assert ev.eligible and ev.blocking == []


def test_engine_flags_a_failing_grade_as_ineligible():
    ev = evaluate_course(facts("D"), bu_start_date=date(2026, 5, 1), today=date(2026, 10, 1))
    assert not ev.eligible and [c.rule for c in ev.blocking] == ["grade"]


def test_engine_flags_a_missed_deadline():
    ev = evaluate_course(facts("A"), bu_start_date=date(2024, 5, 1), today=date(2026, 10, 1))
    assert [c.rule for c in ev.blocking] == ["deadline"]


def test_engine_collects_every_failure_not_just_the_first():
    ev = evaluate_course(facts("F"), bu_start_date=date(2024, 5, 1), today=date(2026, 10, 1))
    assert {c.rule for c in ev.blocking} == {"grade", "deadline"}


def test_engine_is_deterministic():
    args = dict(bu_start_date=date(2026, 5, 1), today=date(2026, 10, 1))
    assert evaluate_course(facts("B+", "3", "QUARTER"), **args) == evaluate_course(facts("B+", "3", "QUARTER"), **args)


def test_target_evaluation_blocks_group_c():
    ev = evaluate_target("CAS CS 350", GROUP_C)
    assert not ev.eligible and ev.converted_credits is None


def test_json_shape_matches_what_the_api_reads():
    ev = evaluate_course(facts(), bu_start_date=None, today=date(2026, 10, 1))
    for item in ev.to_json():
        assert set(item) == {"rule", "label", "pass", "note"}


def test_rules_version_is_set():
    assert RULES_VERSION.count(".") == 2
