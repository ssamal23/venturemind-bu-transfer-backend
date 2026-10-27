"""Postings become a self-contained snapshot, and can record courses matched without a decision.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-27

Adds transcript_course_id (unique), outcome and bu_course_id to postings, makes decision_id
optional (an existing equivalency has no decision), and blocks edits and deletes on postings.

Every step checks whether it is already done. Migration 0001 builds tables from the current
models, so on a brand-new database these columns already exist.
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _columns(table: str) -> dict[str, dict]:
    return {c["name"]: c for c in sa.inspect(op.get_bind()).get_columns(table)}


def _constraints(table: str) -> set[str]:
    insp = sa.inspect(op.get_bind())
    names = {c["name"] for c in insp.get_foreign_keys(table)}
    names |= {c["name"] for c in insp.get_unique_constraints(table)}
    names |= {c["name"] for c in insp.get_check_constraints(table)}
    return names


def upgrade() -> None:
    cols = _columns("postings")
    if "transcript_course_id" not in cols:
        op.add_column("postings", sa.Column("transcript_course_id", sa.Uuid(), nullable=True))
    if "outcome" not in cols:
        op.add_column("postings", sa.Column("outcome", sa.String(32), nullable=True))
    if "bu_course_id" not in cols:
        op.add_column("postings", sa.Column("bu_course_id", sa.Uuid(), nullable=True))

    # Nothing has been posted before this migration, so the new columns can be required.
    if op.get_bind().execute(sa.text("select count(*) from postings")).scalar():
        raise RuntimeError("postings already has rows; backfill transcript_course_id and outcome before running this migration")
    op.alter_column("postings", "transcript_course_id", nullable=False)
    op.alter_column("postings", "outcome", nullable=False)
    op.alter_column("postings", "decision_id", nullable=True)

    existing = _constraints("postings")
    if "fk_postings_transcript_course_id_transcript_courses" not in existing:
        op.create_foreign_key("fk_postings_transcript_course_id_transcript_courses", "postings", "transcript_courses",
                              ["transcript_course_id"], ["id"])
    if "fk_postings_bu_course_id_courses" not in existing:
        op.create_foreign_key("fk_postings_bu_course_id_courses", "postings", "courses", ["bu_course_id"], ["id"])
    if "uq_postings_transcript_course_id" not in existing:
        op.create_unique_constraint("uq_postings_transcript_course_id", "postings", ["transcript_course_id"])
    if "ck_postings_outcome" not in existing:
        # op.f() keeps the name as written; without it the naming convention would prefix it a second time.
        op.create_check_constraint(op.f("ck_postings_outcome"), "postings",
                                   "outcome IN ('BU_EQUIVALENT', 'ELECTIVE', 'NO_CREDIT')")

    # A posting is a record of what was sent to the academic system: block edits and deletes.
    op.execute("DROP TRIGGER IF EXISTS trg_postings_no_update ON postings")
    op.execute(
        "CREATE TRIGGER trg_postings_no_update BEFORE UPDATE OR DELETE ON postings "
        "FOR EACH ROW EXECUTE FUNCTION audit_logs_block_changes()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_postings_no_update ON postings")
    op.drop_constraint(op.f("ck_postings_outcome"), "postings", type_="check")
    op.drop_constraint("uq_postings_transcript_course_id", "postings", type_="unique")
    op.drop_constraint("fk_postings_bu_course_id_courses", "postings", type_="foreignkey")
    op.drop_constraint("fk_postings_transcript_course_id_transcript_courses", "postings", type_="foreignkey")
    op.alter_column("postings", "decision_id", nullable=False)
    op.drop_column("postings", "bu_course_id")
    op.drop_column("postings", "outcome")
    op.drop_column("postings", "transcript_course_id")
