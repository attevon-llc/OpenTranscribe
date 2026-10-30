"""A bulk refresh-token revocation that is staged but not yet audited."""

from __future__ import annotations

from dataclasses import dataclass

from app.auth.audit import audit_logger
from app.auth.audit_events import AuditEventType
from app.auth.audit_events import AuditOutcome


@dataclass(frozen=True)
class PendingBulkRevocation:
    """A bulk revocation whose database changes are staged but not yet audited."""

    user_id: int
    count: int
    user_uuid: str | None
    epoch_stamped: bool

    def record_audit(self, reason: str | None = None) -> None:
        """Write the bulk ``AUTH_TOKEN_REVOKE`` SUCCESS record.

        ``user_id`` names the TARGET, matching :meth:`TokenService.revoke_token`:
        this layer has no request and therefore no actor. It is written even for
        a count of 0, because the epoch stamp still invalidated every outstanding
        access token. FedRAMP AU-2/AU-12: the bulk path used by admin password
        reset, role change, lock, MFA reset, SCIM deactivation and directory sync
        is not routed through ``revoke_token`` and needs its own record.
        """
        audit_logger.log(
            event_type=AuditEventType.AUTH_TOKEN_REVOKE,
            outcome=AuditOutcome.SUCCESS,
            user_id=self.user_id,
            details={
                "scope": "all_user_tokens",
                "sessions_revoked": self.count,
                "reason": reason,
                "user_uuid": self.user_uuid,
                "epoch_stamped": self.epoch_stamped,
            },
        )
