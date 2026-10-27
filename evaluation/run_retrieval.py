"""How often does candidate retrieval put the right BU course in the top results?

    python evaluation/run_retrieval.py            # run from the backend folder

For every source course that has a known BU match (course_matches), search BU courses
by meaning and check whether the matched BU course is in the top 1, 3 and 5.
Ground truth is the workbook's match table. Results go to evaluation/results/retrieval.json.

This measures retrieval only (step before the AI), not the AI's final answer.
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # the backend folder

from sqlalchemy import select  # noqa: E402

from app.db import models as m  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services.retrieval import retrieve_by_vector  # noqa: E402

KS = (1, 3, 5)
OUT = Path(__file__).resolve().parent / "results" / "retrieval.json"
EVALUATED = (m.MatchLevel.LIKELY_EQUIVALENT, m.MatchLevel.PARTIAL_SYLLABUS_REVIEW)


def main() -> None:
    hits: dict[str, dict[int, int]] = defaultdict(lambda: {k: 0 for k in KS})
    totals: dict[str, int] = defaultdict(int)
    misses: list[dict] = []

    with SessionLocal() as db:
        pairs = db.execute(
            select(m.CourseMatch, m.Course, m.Institution.short_name)
            .join(m.Course, m.CourseMatch.source_course_id == m.Course.id)
            .join(m.Institution, m.Course.institution_id == m.Institution.id)
            .where(m.CourseMatch.match_level.in_(EVALUATED), m.CourseMatch.bu_course_id.is_not(None))
        ).all()
        bu_code = {c.id: c.code for c in db.scalars(select(m.Course))}

        for match, source, school in pairs:
            if source.embedding is None:
                raise SystemExit("Courses have no embeddings. Run: python -m app.db.seed_content")
            ranked = [c.course.id for c in retrieve_by_vector(db, list(source.embedding), max(KS))]
            keys = ("all", f"level:{match.match_level.value}", f"school:{school}")
            for key in keys:
                totals[key] += 1
            for k in KS:
                if match.bu_course_id in ranked[:k]:
                    for key in keys:
                        hits[key][k] += 1
            if match.bu_course_id not in ranked[:3]:
                misses.append({
                    "source": f"{school} {source.code}",
                    "expected": bu_code[match.bu_course_id],
                    "level": match.match_level.value,
                    "got_top3": [bu_code[i] for i in ranked[:3]],
                })

    report = {
        key: {"pairs": totals[key], **{f"top{k}": round(hits[key][k] / totals[key], 3) for k in KS}}
        for key in sorted(totals)
    }
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"summary": report, "misses_outside_top3": misses}, indent=2), encoding="utf-8")

    print(f"{'group':44} {'pairs':>5} {'top1':>6} {'top3':>6} {'top5':>6}")
    for key, r in report.items():
        print(f"{key:44} {r['pairs']:>5} {r['top1']:>6.0%} {r['top3']:>6.0%} {r['top5']:>6.0%}")
    print(f"\n{len(misses)} pairs missed the top 3. Details: {OUT}")


if __name__ == "__main__":
    main()
