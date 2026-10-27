import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import ActorType, AuditLog


def write_audit(
    db: Session,
    *,
    actor_type: ActorType,
    actor_id: uuid.UUID | None,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    """Append to audit_logs. The caller commits, so the audit row lands with the change."""
    db.add(
        AuditLog(
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            before=before,
            after=after,
        )
    )
