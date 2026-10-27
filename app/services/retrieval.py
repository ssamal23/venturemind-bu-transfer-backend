"""Find the BU courses closest in meaning to a syllabus or a source course (pgvector, cosine)."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Course, Institution
from app.services.embeddings import Embedder, get_embedder


@dataclass(frozen=True)
class Candidate:
    course: Course
    score: float  # cosine similarity, 1.0 is identical


def retrieve_by_vector(db: Session, vector: list[float], k: int = 5) -> list[Candidate]:
    distance = Course.embedding.cosine_distance(vector)
    rows = db.execute(
        select(Course, distance)
        .join(Institution, Course.institution_id == Institution.id)
        .where(Institution.is_bu.is_(True), Course.embedding.is_not(None))
        .order_by(distance)
        .limit(k)
    ).all()
    return [Candidate(course=c, score=round(1.0 - float(d), 4)) for c, d in rows]


def retrieve_candidates(db: Session, text: str, k: int = 5, embedder: Embedder | None = None) -> list[Candidate]:
    vector = (embedder or get_embedder()).embed([text])[0]
    return retrieve_by_vector(db, vector, k)
