"""Load the synthetic course descriptions and outcomes, then embed every course.

    python -m app.db.seed_content              # load text and (re)compute embeddings
    python -m app.db.seed_content --no-embed   # text only

Run after `python -m app.db.seed`. Safe to run again: it overwrites descriptions and
outcomes from data/synthetic/course_content.txt and recomputes the vectors.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from sqlalchemy import select

from app.config import DATA_DIR
from app.db import models as m
from app.db.session import SessionLocal
from app.services.embeddings import course_text, get_embedder

CONTENT = DATA_DIR / "synthetic" / "course_content.txt"


def parse_content(path: Path = CONTENT) -> dict[str, tuple[str, list[str]]]:
    out: dict[str, tuple[str, list[str]]] = {}
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 3 or not all(parts):
            raise ValueError(f"{path.name} line {n}: expected 'code|description|outcome;outcome'")
        code, description, outcomes = parts
        if code in out:
            raise ValueError(f"{path.name} line {n}: duplicate code {code}")
        out[code] = (description, [o.strip() for o in outcomes.split(";") if o.strip()])
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-embed", action="store_true")
    args = ap.parse_args()

    content = parse_content()
    with SessionLocal() as db:
        courses = list(db.scalars(select(m.Course)))
        missing = [c.code for c in courses if c.code not in content]
        unknown = sorted(set(content) - {c.code for c in courses})
        if missing:
            raise SystemExit(f"No content for: {', '.join(missing)}")
        if unknown:
            print(f"Ignoring content for codes not in the catalog: {', '.join(unknown)}")

        for c in courses:
            c.description, c.learning_outcomes = content[c.code]
        print(f"Updated text for {len(courses)} courses.")

        if not args.no_embed:
            embedder = get_embedder()
            vectors = embedder.embed([course_text(c.title, c.description, c.learning_outcomes) for c in courses])
            for c, v in zip(courses, vectors):
                c.embedding = v
            print(f"Embedded {len(courses)} courses ({embedder.dim} dimensions).")
        db.commit()


if __name__ == "__main__":
    main()
