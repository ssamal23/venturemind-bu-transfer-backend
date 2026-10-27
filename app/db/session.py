from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

engine = create_engine(settings.database_url, pool_size=10, max_overflow=10, pool_pre_ping=True)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request.

    Endpoints call session.commit() explicitly. Anything not committed is rolled
    back when the session closes, including when an HTTPException is raised.
    """
    with SessionLocal() as session:
        yield session
