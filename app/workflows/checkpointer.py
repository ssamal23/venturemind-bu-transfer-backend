"""Where workflow checkpoints are stored.

Production: PostgreSQL (the same database), so a paused course survives a restart or a
redeploy. Tests: an in-memory saver, so nothing is written to the real database.
"""

from __future__ import annotations

import logging

from langgraph.checkpoint.base import BaseCheckpointSaver

from app.config import settings

log = logging.getLogger(__name__)
_checkpointer: BaseCheckpointSaver | None = None
_pool = None


def _conninfo() -> str:
    # SQLAlchemy writes "postgresql+psycopg://"; the connection pool wants plain libpq.
    return settings.database_url.replace("postgresql+psycopg://", "postgresql://", 1)


def get_checkpointer() -> BaseCheckpointSaver:
    global _checkpointer, _pool
    if _checkpointer is None:
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        _pool = ConnectionPool(
            conninfo=_conninfo(), max_size=4, open=True,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        )
        saver = PostgresSaver(_pool)
        if settings.checkpoint_auto_setup:
            saver.setup()  # creates the checkpoint tables the first time; does nothing after that
        _checkpointer = saver
        log.info("Workflow checkpoints are stored in PostgreSQL")
    return _checkpointer


def set_checkpointer(saver: BaseCheckpointSaver | None) -> None:
    """Replace the saver (tests pass an in-memory one; None resets so the next call rebuilds it)."""
    global _checkpointer
    _checkpointer = saver


def close() -> None:
    global _checkpointer, _pool
    _checkpointer = None
    if _pool is not None:
        _pool.close()
        _pool = None


def main() -> None:
    """Create the checkpoint tables. Run once as the database admin: python -m app.workflows.checkpointer"""
    settings.checkpoint_auto_setup = True
    get_checkpointer()
    close()
    print("Workflow checkpoint tables are ready.")


if __name__ == "__main__":
    main()
