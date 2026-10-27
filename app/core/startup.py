"""Refuse to start a production deployment with an unsafe configuration."""

from app.config import Settings

DEFAULT_SECRET_PREFIX = "dev-only"
MIN_SECRET_LENGTH = 32


def production_problems(s: Settings) -> list[str]:
    """What is wrong with these settings for a production run. An empty list means none."""
    if s.environment != "production":
        return []
    problems = []
    if s.jwt_secret.startswith(DEFAULT_SECRET_PREFIX) or len(s.jwt_secret) < MIN_SECRET_LENGTH:
        problems.append(f"JWT_SECRET must be a random value of at least {MIN_SECRET_LENGTH} characters, not the development default")
    if s.storage_backend != "azure":
        problems.append("STORAGE_BACKEND must be azure: a container's own disk is private to it and lost on restart")
    elif not (s.azure_storage_account_url or s.azure_storage_connection_string):
        problems.append("AZURE_STORAGE_ACCOUNT_URL is not set")
    if "*" in s.cors_origins:
        problems.append("CORS_ORIGINS must not contain *")
    if "@localhost" in s.database_url or "@127.0.0.1" in s.database_url:
        problems.append("DATABASE_URL points at localhost")
    if "sslmode=require" not in s.database_url and "sslmode=verify" not in s.database_url:
        problems.append("DATABASE_URL must use sslmode=require")
    if s.force_primary_fail:
        problems.append("FORCE_PRIMARY_FAIL is a demo switch and must be off")
    return problems


def check_production(s: Settings) -> None:
    problems = production_problems(s)
    if problems:
        raise RuntimeError("Unsafe production configuration:\n  - " + "\n  - ".join(problems))
