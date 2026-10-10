"""What the platform-admin role may reach on one request (issue #1122).

``User.is_admin`` used to be checked in front of every tenant gate, so a platform admin could
read, edit and delete any tenant's content by UUID. Sites now load the row first and ask
``PlatformBypass.allows`` with the row's own tenant, so the decision is made against the
resource and never in front of it.

* single-tenant mode: an admin keeps today's instance-wide access, exactly.
* multi-tenant mode: no implicit content access at all.

The default instance, ``PlatformBypass.none()``, allows nothing, so a context built without
one fails closed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING
from typing import Literal

from sqlalchemy import exists
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.services.platform_access import TenancyMode
from app.services.platform_access import tenancy_mode

if TYPE_CHECKING:
    from app.models.user import User

logger = logging.getLogger(__name__)

Need = Literal["read", "write"]


def _forced_single() -> bool:
    return (settings.TENANCY_MODE or "").strip().lower() == "single"


def _active_orgs_exist(db: Session) -> bool:
    from app.models.organization import Organization

    try:
        return bool(db.scalar(select(exists().where(Organization.is_active.is_(True)))))
    except Exception:
        logger.warning("organization probe failed; auditing cross-tenant bypass", exc_info=True)
        return True


@dataclass(frozen=True)
class SupportGrantView:
    """Frozen snapshot of a grant that passed ``resolve_active_grant`` for this request."""

    id: int
    uuid: str
    organization_id: int | None
    subject_user_id: int | None
    access_level: Literal["read", "write"]
    grant_mode: Literal["approved", "break_glass"]
    expires_at: datetime

    @property
    def is_org_grant(self) -> bool:
        return self.organization_id is not None


@dataclass(frozen=True)
class PlatformBypass:
    """The platform-admin reach for this request. See the module docstring."""

    user: User | None = None
    mode: TenancyMode = TenancyMode.MULTI
    #: The caller's own tenant, used only to decide whether a use crossed tenants.
    caller_org_id: int | None = None
    #: ``TENANCY_MODE=single`` forced over a deployment that has active organizations.
    audit_cross_tenant: bool = False
    #: The support-access grant this request carries, already validated. Multi mode only.
    grant: SupportGrantView | None = None
    #: Request identity stamped on every use row written under ``grant``.
    method: str = ""
    route: str = ""
    _audited: set = field(default_factory=set, compare=False, repr=False)

    @classmethod
    def none(cls) -> PlatformBypass:
        return cls()

    @property
    def under_grant(self) -> bool:
        return self.grant is not None

    @property
    def user_is_admin(self) -> bool:
        """Quarantine-review visibility only (class Q); never widens tenant scope."""
        return bool(self.user is not None and self.user.is_admin)

    @property
    def sees_all_in_scope(self) -> bool:
        """List endpoints: may this caller see every user's rows inside the active scope.

        Single mode: every admin. Multi mode: only an ORG grant, whose requests assume the
        tenant, so a list is the tenant's own. A personal grant reaches rows by uuid only.
        """
        if self.mode is TenancyMode.SINGLE:
            return self.user_is_admin
        return self.grant is not None and self.grant.is_org_grant

    def presign_ttl(self, default_seconds: int) -> int:
        """Lifetime for a browser-facing presigned URL minted on this request.

        A presigned URL is a bearer token and outlives a revoked grant, so under a grant it
        is capped at ``SUPPORT_ACCESS_PRESIGN_MAX_SECONDS`` and at the grant's own expiry,
        floored at the storage layer's 60 s clamp.
        """
        if self.grant is None:
            return default_seconds
        from app.core.constants import SUPPORT_ACCESS_PRESIGN_MAX_SECONDS

        remaining = int((self.grant.expires_at - datetime.now(UTC)).total_seconds())
        return max(60, min(default_seconds, SUPPORT_ACCESS_PRESIGN_MAX_SECONDS, remaining))

    @property
    def admin_reveal_allowed(self) -> bool:
        """Unredacted reveal is owner-only in multi-tenant mode; admins keep it in single."""
        return self.mode is TenancyMode.SINGLE and self.user_is_admin

    def allows(
        self,
        *,
        org_id: int | None,
        owner_id: int | None,
        need: Need,
        resource_type: str,
        resource_uuid: str,
    ) -> bool:
        """May the platform role act on a row stamped ``org_id`` and owned by ``owner_id``."""
        if not self.user_is_admin:
            return False
        if self.mode is TenancyMode.SINGLE:
            if self.audit_cross_tenant:
                self._audit_cross_tenant(org_id, owner_id, need, resource_type, resource_uuid)
            return True
        return self._allows_under_grant(org_id, owner_id, need, resource_type, resource_uuid)

    def _allows_under_grant(
        self,
        org_id: int | None,
        owner_id: int | None,
        need: Need,
        resource_type: str,
        resource_uuid: str,
    ) -> bool:
        """Multi-tenant mode: only the granted tenant, and only as far as the grant goes.

        Records one use row per distinct resource and need per request, written BEFORE the
        answer is returned; if the row cannot be written this raises 503 and nothing is
        served unrecorded.
        """
        grant = self.grant
        if grant is None:
            return False
        if grant.is_org_grant:
            in_tenant = org_id == grant.organization_id
        else:
            in_tenant = (
                grant.subject_user_id is not None
                and org_id is None
                and owner_id == grant.subject_user_id
            )
        if not in_tenant or (need == "write" and grant.access_level != "write"):
            return False
        key = ("use", resource_type, resource_uuid, need)
        if key not in self._audited:
            from app.services.support_access_service import record_use

            record_use(
                grant,
                actor_user_id=int(self.user.id) if self.user is not None else None,
                method=self.method,
                route=self.route,
                resource_type=resource_type,
                resource_uuid=resource_uuid,
                need=need,
                organization_id=org_id,
                owner_user_id=owner_id,
            )
            self._audited.add(key)
        return True

    def _audit_cross_tenant(
        self,
        org_id: int | None,
        owner_id: int | None,
        need: Need,
        resource_type: str,
        resource_uuid: str,
    ) -> None:
        """Audit a forced-single bypass across tenants, once per resource per request."""
        if self.user is None:
            return
        if org_id is not None:
            same_tenant = org_id == self.caller_org_id
        else:
            same_tenant = self.caller_org_id is None and owner_id == self.user.id
        key = (resource_type, resource_uuid, need)
        if same_tenant or key in self._audited:
            return
        self._audited.add(key)
        from app.auth.audit import AuditEventType
        from app.auth.audit import AuditOutcome
        from app.auth.audit import audit_logger

        audit_logger.log(
            AuditEventType.PLATFORM_ADMIN_CONTENT_ACCESS,
            AuditOutcome.SUCCESS,
            user_id=self.user.id,
            organization_id=org_id,
            target_user_id=owner_id,
            details={"resource_type": resource_type, "resource_uuid": resource_uuid, "need": need},
        )


def audit_metadata_access(
    db: Session,
    user: User,
    *,
    route: str,
    count: int,
    organization_ids: Iterable[int | None],
) -> bool:
    """Record that a platform route returned other tenants' metadata (filenames, owners).

    Only multi-tenant mode emits it: in single-tenant mode the same listing is ordinary
    instance administration. One event per response, ids and counts only, never filenames.

    Returns:
        True when an event was emitted.
    """
    if tenancy_mode(db) is not TenancyMode.MULTI:
        return False
    from app.auth.audit import AuditEventType
    from app.auth.audit import AuditOutcome
    from app.auth.audit import audit_logger

    audit_logger.log(
        AuditEventType.PLATFORM_ADMIN_METADATA_ACCESS,
        AuditOutcome.SUCCESS,
        user_id=user.id,
        details={
            "route": route,
            "count": count,
            "organization_ids": sorted({o for o in organization_ids if o is not None}),
        },
    )
    return True


def build_bypass(
    db: Session,
    user: User,
    org_id: int | None,
    *,
    grant: SupportGrantView | None = None,
    method: str = "",
    route: str = "",
) -> PlatformBypass:
    """The ``PlatformBypass`` for ``user`` acting in tenant ``org_id`` (None = personal).

    A ``grant`` is only ever passed by ``resolve_request_bypass`` after it validated the
    header, which it refuses outside multi-tenant mode; so a grant implies multi mode.
    """
    if not user.is_admin:
        return PlatformBypass(user=user, caller_org_id=org_id)
    if grant is not None:
        return PlatformBypass(
            user=user,
            mode=TenancyMode.MULTI,
            caller_org_id=org_id,
            grant=grant,
            method=method,
            route=route,
        )
    mode = tenancy_mode(db)
    audit = mode is TenancyMode.SINGLE and _forced_single() and _active_orgs_exist(db)
    return PlatformBypass(user=user, mode=mode, caller_org_id=org_id, audit_cross_tenant=audit)
