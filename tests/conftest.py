"""API tests run against the seeded database inside a transaction that is rolled back.

Nothing a test writes is kept. The demo data must already be seeded
(python -m app.db.seed, then python -m app.db.seed_demo).
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import engine, get_session
from app.main import app
from app.workflows.checkpointer import set_checkpointer


@pytest.fixture()
def db():
    connection = engine.connect()
    outer = connection.begin()
    # commit() inside the app releases a savepoint; the outer transaction is never committed
    session = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        connection.close()


@pytest.fixture()
def client(db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    app.dependency_overrides[get_session] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


def login(client: TestClient, email: str, password: str = "demo1234") -> dict:
    res = client.post("/api/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, res.text
    body = res.json()
    return {"headers": {"Authorization": f"Bearer {body['token']}"}, "user": body["user"]}


@pytest.fixture()
def student(client):
    return login(client, "student@demo.edu")


@pytest.fixture()
def evaluator(client):
    return login(client, "evaluator@demo.edu")


@pytest.fixture(autouse=True)
def memory_checkpointer():
    """Workflow checkpoints go to memory in tests, never to the real database."""
    from langgraph.checkpoint.memory import InMemorySaver

    saver = InMemorySaver()
    set_checkpointer(saver)
    yield saver
    set_checkpointer(None)
