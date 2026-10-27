import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = BACKEND_ROOT / ".env"  # backend/.env, whatever the working directory
# Seed data (the workbook and the course descriptions). Override with the DATA_DIR environment variable.
DATA_DIR = Path(os.environ.get("DATA_DIR") or BACKEND_ROOT / "data")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    # "production" turns on startup checks that refuse an unsafe configuration (app/core/startup.py).
    environment: str = "development"

    database_url: str = "postgresql+psycopg://bu:bu@localhost:5432/transfer"
    # all-MiniLM-L6-v2 produces 384-dimensional vectors; change with the embedding model
    embedding_dim: int = 384

    # Signs login tokens. Set a long random value in .env; the default is for local runs only.
    jwt_secret: str = "dev-only-change-me-use-a-long-random-value-in-env"
    jwt_minutes: int = 480

    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:5174"]

    # Uploaded files. "local" writes under storage_dir; "azure" uses Blob Storage (needed when the
    # API and the worker run in separate containers). Azure sign-in is by managed identity.
    storage_backend: str = "local"
    storage_dir: str = "storage"
    azure_storage_account_url: str = ""  # https://<account>.blob.core.windows.net
    azure_storage_connection_string: str = ""  # local testing only
    max_upload_mb: int = 10

    anthropic_api_key: str = ""
    gemini_api_key: str = ""

    # LLM gateway. Tried in this order; a provider with no key is skipped.
    # Names: claude, claude_small, gemini
    llm_chain: list[str] = ["claude", "gemini"]
    claude_model: str = "claude-sonnet-5-5"
    claude_small_model: str = "claude-haiku-5-5"
    # Check the current free models in Google AI Studio; names change.
    gemini_model: str = "gemini-2.5-flash"
    # Demo switch: pretend the first provider is down, to show the failover live.
    force_primary_fail: bool = False

    worker_max_attempts: int = 4

    # Create the workflow checkpoint tables at startup. Turn off in Azure, where the app's database
    # user cannot create tables; run `python -m app.workflows.checkpointer` once as the admin instead.
    checkpoint_auto_setup: bool = True


settings = Settings()
