"""Rename the postings outcome check constraint to match the model.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-27

Migration 0003 first created this constraint as ck_postings_ck_postings_outcome (the naming
convention was applied to a name that already carried the prefix). 0003 now creates it
correctly. This renames it in databases that already ran the first version, and does nothing
elsewhere.
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    names = {c["name"] for c in sa.inspect(op.get_bind()).get_check_constraints("postings")}
    if "ck_postings_ck_postings_outcome" in names and "ck_postings_outcome" not in names:
        op.execute("ALTER TABLE postings RENAME CONSTRAINT ck_postings_ck_postings_outcome TO ck_postings_outcome")


def downgrade() -> None:
    pass  # the old name was a mistake; there is nothing to go back to
