import pytest

from app.config import Settings
from app.core.startup import check_production, production_problems

GOOD = dict(
    environment="production",
    jwt_secret="x" * 48,
    storage_backend="azure",
    azure_storage_account_url="https://acct.blob.core.windows.net",
    cors_origins=["https://bu-web.example.azurecontainerapps.io"],
    database_url="postgresql+psycopg://bu_app:pw@db.postgres.database.azure.com:5432/transfer?sslmode=require",
)


def settings(**over):
    return Settings(_env_file=None, **{**GOOD, **over})


def test_a_correct_production_configuration_passes():
    assert production_problems(settings()) == []
    check_production(settings())  # does not raise


def test_development_is_never_blocked():
    dev = Settings(_env_file=None, environment="development")  # all defaults: local disk, dev secret, localhost
    assert production_problems(dev) == []


@pytest.mark.parametrize("override, expected", [
    ({"jwt_secret": "dev-only-change-me-use-a-long-random-value-in-env"}, "JWT_SECRET"),
    ({"jwt_secret": "short"}, "JWT_SECRET"),
    ({"storage_backend": "local"}, "STORAGE_BACKEND"),
    ({"azure_storage_account_url": ""}, "AZURE_STORAGE_ACCOUNT_URL"),
    ({"cors_origins": ["*"]}, "CORS_ORIGINS"),
    ({"database_url": "postgresql+psycopg://u:p@localhost:5432/transfer?sslmode=require"}, "localhost"),
    ({"database_url": "postgresql+psycopg://u:p@db.postgres.database.azure.com:5432/transfer"}, "sslmode"),
    ({"force_primary_fail": True}, "FORCE_PRIMARY_FAIL"),
])
def test_each_unsafe_setting_is_named(override, expected):
    problems = production_problems(settings(**override))
    assert len(problems) == 1 and expected in problems[0]
    with pytest.raises(RuntimeError, match="Unsafe production configuration"):
        check_production(settings(**override))


def test_every_problem_is_reported_at_once():
    bad = settings(jwt_secret="short", storage_backend="local", cors_origins=["*"])
    assert len(production_problems(bad)) == 3
