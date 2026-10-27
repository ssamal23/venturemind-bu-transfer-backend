from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import auth, catalog, decisions, health, postings, review_queue, students, syllabi, transcripts
from app.config import settings
from app.core.startup import check_production


@asynccontextmanager
async def lifespan(_app: FastAPI):
    check_production(settings)  # raises if ENVIRONMENT=production with an unsafe configuration
    yield


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
