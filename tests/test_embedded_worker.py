"""The worker loop, and running it inside the API process. No database needed."""

import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.workers import evaluation_worker as ew


class FakeSession:
    def __enter__(self):
        return object()

    def __exit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    monkeypatch.setattr(ew, "SessionLocal", lambda: FakeSession())


def script(monkeypatch, outcomes):
    """Make run_once return (or raise) the given outcomes in order, then None forever."""
    queue = list(outcomes)
    calls = []

    def fake(db, worker_id="worker", **kw):
        calls.append(worker_id)
        step = queue.pop(0) if queue else None
        if isinstance(step, Exception):
            raise step
        return step

    monkeypatch.setattr(ew, "run_once", fake)
    return calls


def test_the_loop_handles_jobs_and_stops_when_the_queue_is_empty(monkeypatch):
    calls = script(monkeypatch, ["suggested", "parsed", "requeued"])
    assert ew.run_loop(threading.Event(), "w1", interval=0, once=True) == 3
    assert calls == ["w1"] * 4  # three jobs, then the empty check


def test_a_failure_is_logged_and_retried_not_fatal(monkeypatch):
    calls = script(monkeypatch, [ConnectionError("database unreachable"), "suggested"])
    monkeypatch.setattr(ew.threading.Event, "wait", lambda self, timeout=None: False)  # skip the pause
    assert ew.run_loop(threading.Event(), "w1", interval=0, once=True) == 1
    assert len(calls) == 3  # the failed attempt, the job, the empty check


def test_a_stop_request_ends_the_loop_at_once(monkeypatch):
    calls = script(monkeypatch, ["suggested"] * 5)
    stop = threading.Event()
    stop.set()
    assert ew.run_loop(stop, "w1", interval=0) == 0 and calls == []


def test_an_idle_loop_waits_and_wakes_when_stopped(monkeypatch):
    script(monkeypatch, [])
    stop = threading.Event()
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("n", ew.run_loop(stop, "w1", interval=30)))
    t.start()
    time.sleep(0.2)
    assert t.is_alive()  # waiting between polls
    started = time.monotonic()
    stop.set()
    t.join(timeout=5)
    assert not t.is_alive() and time.monotonic() - started < 2 and result["n"] == 0  # did not sleep the full 30 s


# ---------------------------------------------------------------- inside the API process
def test_the_api_starts_and_stops_the_embedded_worker(monkeypatch):
    from app.main import app

    state = {"started": False, "stopped": False, "id": None}

    def fake_loop(stop, worker_id, interval=5.0, once=False):
        state.update(started=True, id=worker_id)
        stop.wait(10)
        state["stopped"] = stop.is_set()
        return 0

    monkeypatch.setattr(settings, "embedded_worker", True)
    monkeypatch.setattr(ew, "run_loop", fake_loop)
    with TestClient(app):
        for _ in range(50):
            if state["started"]:
                break
            time.sleep(0.05)
        assert state["started"] and state["id"].startswith("embedded-")
        assert any(t.name == "embedded-worker" and t.is_alive() for t in threading.enumerate())
    time.sleep(0.2)
    assert state["stopped"]  # shutting the API down told the worker to stop
    assert not any(t.name == "embedded-worker" and t.is_alive() for t in threading.enumerate())


def test_the_worker_stays_off_unless_asked(monkeypatch):
    from app.main import app

    started = []
    monkeypatch.setattr(settings, "embedded_worker", False)
    monkeypatch.setattr(ew, "run_loop", lambda *a, **k: started.append(1))
    with TestClient(app):
        time.sleep(0.2)
    assert started == [] and not any(t.name == "embedded-worker" for t in threading.enumerate())
