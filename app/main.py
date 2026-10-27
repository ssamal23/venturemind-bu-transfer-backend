import logging
import os
import socket
import threading
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import auth, catalog, decisions, health, postings, review_queue, students, syllabi, transcripts
from app.config import settings
from app.core.startup import check_production

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    check_production(settings)  # raises if ENVIRONMENT=production with an unsafe configuration
    stop = threading.Event()
    thread = None
    if settings.embedded_worker:
        from app.workers import evaluation_worker  # imported here so the API alone does not load the AI libraries

        worker_id = f"embedded-{socket.gethostname()}-{os.getpid()}"
        thread = threading.Thread(
            target=evaluation_worker.run_loop, args=(stop, worker_id, settings.worker_poll_seconds),
            name="embedded-worker", daemon=True,
        )
        thread.start()
        log.info("Embedded worker started (%s)", worker_id)
    try:
        yield
    finally:
        stop.set()  # ask the worker to finish its current job and stop
        if thread is not None:
            thread.join(timeout=20)


app = FastAPI(title="BU transfer credit API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
)

api = APIRouter(prefix="/api")
for module in (health, auth, catalog, students, transcripts, syllabi, review_queue, decisions, postings):
    api.include_router(module.router)
app.include_router(api)
