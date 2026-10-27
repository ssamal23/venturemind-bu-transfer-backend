"""Load the hackathon workbook and demo users into the database.

    python -m app.db.seed                 # load into DATABASE_URL
    python -m app.db.seed --dry-run       # parse the workbook, print counts, touch nothing
    python -m app.db.seed --xlsx PATH     # use a different workbook

Safe to run twice: every insert checks for an existing row first.
The workbook has no course descriptions or syllabi, so embeddings stay empty
until the synthetic descriptions are generated.
"""

from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path
from typing import Any

import openpyxl

from app.config import DATA_DIR

DEFAULT_XLSX = DATA_DIR / "BU_Transfer_Program_Course_Match.xlsx"
DEMO_PASSWORD = "demo1234"

INSTITUTIONS = [
    ("Boston University", "BU", "SEMESTER", True),
    ("Northeastern University", "Northeastern", "SEMESTER", False),
    ("New York University", "NYU", "SEMESTER", False),
    ("Lakeview College", "Lakeview", "QUARTER", False),  # synthetic quarter-system school
    ("Community College X", "CCX", "SEMESTER", False),  # synthetic community college
]

CATALOG_SHEETS = {
    "BU Courses": "Boston University",
    "Northeastern Courses": "Northeastern University",
    "NYU Courses": "New York University",
}

MATCH_LEVELS = {
    "likely equivalent": "LIKELY_EQUIVALENT",
    "partial - syllabus review": "PARTIAL_SYLLABUS_REVIEW",
    "no equivalent found": "NO_EQUIVALENT",
    "must be taken at bu": "MUST_TAKE_AT_BU",
}

# Structured versions of the "BU key rules" text, written by hand from the workbook.
# kind: (requirement_type, program_area, course codes or None, group, max_count, min_level, description, params)
REQUIREMENTS: list[dict[str, Any]] = [
    *[
        {
            "program": "Computer Science",
            "type": "NON_TRANSFERABLE",
            "code": code,
            "group": "Group C",
            "description": "Group C courses (CS 320, 332, 350) cannot be transferred.",
        }
        for code in ("CAS CS 320", "CAS CS 332", "CAS CS 350")
    ],
    {
        "program": "Economics",
        "type": "MAX_TRANSFER",
        "max_count": 4,
        "min_level": 200,
        "description": "At most 4 courses at the 200 level or above may be transferred after matriculation.",
    },
    *[
        {
            "program": "Psychology",
            "type": "REQUIRED_COURSE",
            "code": code,
            "description": f"{code} is required for the BA in Psychology.",
        }
        for code in ("CAS PS 101", "CAS PS 231")
    ],
    {
        "program": "Biology",
        "type": "MUST_TAKE_AT_BU",
        "max_count": 5,
        "description": "At least 5 BI courses beyond BI 107/108 must be taken at BU.",
        "params": {"course_prefix": "BI", "beyond": ["BI 107", "BI 108"]},
    },
    *[
        {
            "program": "Mathematics",
            "type": "REQUIRED_COURSE",
            "code": code,
            "description": f"{code} is part of the required calculus sequence.",
        }
        for code in ("CAS MA 123", "CAS MA 124", "CAS MA 225", "CAS MA 242")
    ],
]

DEMO_USERS = [
    ("student@demo.edu", "Alex Student", "STUDENT", None, "STU-0001"),
    ("evaluator@demo.edu", "Dr. Rivera", "EVALUATOR", "Chemistry", None),
    ("office@demo.edu", "Credit Office Staff", "CREDIT_OFFICE", None, None),
]


# --------------------------------------------------------------------------- parsing (no database)
def read_sheet(wb: openpyxl.Workbook, name: str) -> list[dict[str, Any]]:
    rows = list(wb[name].iter_rows(values_only=True))
    header = rows[0]
    return [dict(zip(header, r)) for r in rows[1:] if any(c is not None for c in r)]


def parse_workbook(path: Path) -> dict[str, Any]:
    wb = openpyxl.load_workbook(path, data_only=True)
    data: dict[str, Any] = {
        "programs": [r for r in read_sheet(wb, "Programs") if r.get("Program area")],
        "courses": {},
        "matches": [],
    }
    for sheet, school in CATALOG_SHEETS.items():
        data["courses"][school] = [r for r in read_sheet(wb, sheet) if r.get("Course code") and r.get("Credits") is not None]

    catalogs = {s: {str(r["Course code"]).strip() for r in rows} for s, rows in data["courses"].items()}
    bu = catalogs["Boston University"]
    for row in read_sheet(wb, "Course Match"):
        bu_code = str(row.get("BU course") or "").strip()
        if bu_code not in bu:
            continue  # the sheet ends with a note row
        for school, code_col, level_col in (
            ("Northeastern University", "Northeastern course", "NU match level"),
            ("New York University", "NYU course", "NYU match level"),
        ):
            source = str(row.get(code_col) or "").strip()
            level = MATCH_LEVELS.get(str(row.get(level_col) or "").strip().lower())
            if source in catalogs[school] and level:
                data["matches"].append(
                    {"school": school, "source": source, "bu": bu_code, "level": level, "notes": row.get("Notes")}
                )
    return data


# --------------------------------------------------------------------------- database loading
def load(data: dict[str, Any]) -> dict[str, int]:
    from sqlalchemy import select

    from app.core.security import hash_password
    from app.db import models as m
    from app.db.session import SessionLocal

    counts = {"institutions": 0, "programs": 0, "courses": 0, "course_matches": 0, "program_requirements": 0, "users": 0}

    with SessionLocal() as db, db.begin():
        inst: dict[str, m.Institution] = {}
        for name, short, cal, is_bu in INSTITUTIONS:
            row = db.scalar(select(m.Institution).where(m.Institution.name == name))
            if not row:
                row = m.Institution(name=name, short_name=short, calendar_type=m.CalendarType(cal), is_bu=is_bu)
                db.add(row)
                counts["institutions"] += 1
            inst[name] = row
        db.flush()

        programs: dict[str, m.Program] = {}
        for r in data["programs"]:
            area = str(r["Program area"]).strip()
            row = db.scalar(select(m.Program).where(m.Program.program_area == area))
            if not row:
                row = m.Program(
                    program_area=area,
                    bu_program=r.get("BU program") or area,
                    bu_school=r.get("BU school") or "",
                    rules_summary=r.get("BU key rules"),
                    bu_source_url=r.get("BU source"),
                    program_match=r.get("Program match"),
                    notes=r.get("Notes"),
                )
                db.add(row)
                counts["programs"] += 1
            programs[area] = row
        db.flush()

        courses: dict[tuple[str, str], m.Course] = {}
        for school, rows in data["courses"].items():
            for r in rows:
                code = str(r["Course code"]).strip()
                row = db.scalar(
                    select(m.Course).where(m.Course.institution_id == inst[school].id, m.Course.code == code)
                )
                if not row:
                    row = m.Course(
                        institution_id=inst[school].id,
                        program_id=programs[r["Program"]].id if r.get("Program") in programs else None,
                        code=code,
                        title=str(r["Course title"]).strip(),
                        requirement_type=r.get("Requirement type"),
                        credits=Decimal(str(r["Credits"])),
                        verified=str(r.get("Verified") or "").strip().lower() == "yes",
                        notes=r.get("Notes"),
                        source_url=r.get("Source"),
                    )
                    db.add(row)
                    counts["courses"] += 1
                courses[(school, code)] = row
        db.flush()

        bu = "Boston University"
        for mt in data["matches"]:
            src, dst = courses[(mt["school"], mt["source"])], courses[(bu, mt["bu"])]
            exists = db.scalar(
                select(m.CourseMatch).where(
                    m.CourseMatch.source_course_id == src.id, m.CourseMatch.bu_course_id == dst.id
                )
            )
            if not exists:
                db.add(
                    m.CourseMatch(
                        source_course_id=src.id,
                        bu_course_id=dst.id,
                        match_level=m.MatchLevel(mt["level"]),
                        notes=mt["notes"],
                    )
                )
                counts["course_matches"] += 1

        for req in REQUIREMENTS:
            program = programs.get(req["program"])
            if not program:
                continue
            course = courses.get((bu, req["code"])) if req.get("code") else None
            exists = db.scalar(
                select(m.ProgramRequirement).where(
                    m.ProgramRequirement.program_id == program.id,
                    m.ProgramRequirement.requirement_type == m.RequirementType(req["type"]),
                    m.ProgramRequirement.course_id == (course.id if course else None),
                    m.ProgramRequirement.description == req["description"],
                )
            )
            if not exists:
                db.add(
                    m.ProgramRequirement(
                        program_id=program.id,
                        requirement_type=m.RequirementType(req["type"]),
                        course_id=course.id if course else None,
                        group_label=req.get("group"),
                        max_count=req.get("max_count"),
                        min_course_level=req.get("min_level"),
                        description=req["description"],
                        rule_params=req.get("params", {}),
                        source_url=program.bu_source_url,
                    )
                )
                counts["program_requirements"] += 1

        for email, name, role, dept, ref in DEMO_USERS:
            if not db.scalar(select(m.User).where(m.User.email == email)):
                db.add(
                    m.User(
                        email=email,
                        name=name,
                        role=m.Role(role),
                        department=dept,
                        student_ref=ref,
                        password_hash=hash_password(DEMO_PASSWORD),
                    )
                )
                counts["users"] += 1
    return counts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX)
    ap.add_argument("--dry-run", action="store_true", help="parse the workbook and print counts only")
    args = ap.parse_args()

    data = parse_workbook(args.xlsx)
    summary = {
        "programs": len(data["programs"]),
        "courses": {s: len(r) for s, r in data["courses"].items()},
        "course_matches": len(data["matches"]),
        "program_requirements": len(REQUIREMENTS),
    }
    print("Parsed workbook:", summary)
    if args.dry_run:
        return
    print("Inserted:", load(data))


if __name__ == "__main__":
    main()
