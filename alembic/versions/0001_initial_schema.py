"""Initial schema: pgvector extension, 19 tables, append-only triggers.

Revision ID: 0001
Revises:
Create Date: 2026-10-26

This first revision builds the tables from the current models with
create_all so it always matches them. Every later revision must be an
explicit migration (op.add_column and so on), because importing the live
models would no longer describe the past.
"""
from pathlib import Path

from alembic import op

from app.db.models import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[2] / "app" / "db" / "sql"


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    Base.metadata.create_all(bind=bind)
    op.execute((SQL_DIR / "audit_append_only.sql").read_text(encoding="utf-8"))


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_audit_logs_append_only ON audit_logs")
    op.execute("DROP TRIGGER IF EXISTS trg_decisions_no_update ON evaluation_decisions")
    Base.metadata.drop_all(bind=op.get_bind())
    op.execute("DROP FUNCTION IF EXISTS audit_logs_block_changes()")
