"""Jobs can point at a transcript (parse_transcript jobs).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-27

Each step checks whether it is already done. Migration 0001 builds tables from the current
models, so on a brand-new database this column already exists.
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "transcript_id" not in {c["name"] for c in insp.get_columns("jobs")}:
        op.add_column("jobs", sa.Column("transcript_id", sa.Uuid(), nullable=True))
    if "fk_jobs_transcript_id_transcripts" not in {f["name"] for f in insp.get_foreign_keys("jobs")}:
        op.create_foreign_key("fk_jobs_transcript_id_transcripts", "jobs", "transcripts", ["transcript_id"], ["id"])
    if "ix_jobs_transcript_id" not in {i["name"] for i in insp.get_indexes("jobs")}:
        op.create_index("ix_jobs_transcript_id", "jobs", ["transcript_id"])


def downgrade() -> None:
    op.drop_index("ix_jobs_transcript_id", table_name="jobs")
    op.drop_constraint("fk_jobs_transcript_id_transcripts", "jobs", type_="foreignkey")
    op.drop_column("jobs", "transcript_id")
