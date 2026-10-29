"""Audit record for an admin-user write that a precondition refused.

Lives beside, not inside, ``auth/audit.py`` (already over the file-size cap).
"""

from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.models.user import User


def audit_user_update_refusal(
    action: str, user: User, current_user: User, client_ip: str, user_agent: str
) -> None:
    """Record a refusal at a privilege boundary of an admin user-management write.

    Reuses ``ADMIN_USER_UPDATE`` with ``outcome=FAILURE`` rather than a new
    ``AuditEventType`` — the endpoints' successful writes already emit that type, so
    a refusal is the same event with a different outcome and an ``action`` naming
    which boundary or precondition stopped it. Actor vs. subject follows issue #443:
    ``user_id``/``username`` are the caller, ``target_user_id``/``target_username``
    are the account the write was attempted against.
    """
    audit_logger.log(
        event_type=AuditEventType.ADMIN_USER_UPDATE,
        outcome=AuditOutcome.FAILURE,
        user_id=current_user.id,
        username=str(current_user.email),
        target_user_id=int(user.id),
        target_username=str(user.email),
        source_ip=client_ip,
        user_agent=user_agent,
        details={"action": action},
    )
