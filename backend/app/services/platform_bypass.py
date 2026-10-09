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
class PlatformBypass:
    """The platform-admin reach for this request. See the module docstring."""

    user: User | None = None
    mode: TenancyMode = TenancyMode.MULTI
    #: The caller's own tenant, used only to decide whether a use crossed tenants.
    caller_org_id: int | None = None
    #: ``TENANCY_MODE=single`` forced over a deployment that has active organizations.
    audit_cross_tenant: bool = False
    _audited: set = field(default_factory=set, compare=False, repr=False)

    @classmethod
    def none(cls) -> PlatformBypass:
        return cls()

    @property
    def user_is_admin(self) -> bool:
        """Quarantine-review visibility only (class Q); never widens tenant scope."""
        return bool(self.user is not None and self.user.is_admin)

    @property
    def sees_all_in_scope(self) -> bool:
        """List endpoints: may this caller see every user's rows inside the active scope."""
        return self.mode is TenancyMode.SINGLE and self.user_is_admin

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
        if not self.user_is_admin or self.mode is not TenancyMode.SINGLE:
            return False
        if self.audit_cross_tenant:
            self._audit_cross_tenant(org_id, owner_id, need, resource_type, resource_uuid)
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


def build_bypass(db: Session, user: User, org_id: int | None) -> PlatformBypass:
    """The ``PlatformBypass`` for ``user`` acting in tenant ``org_id`` (None = personal)."""
    if not user.is_admin:
        return PlatformBypass(user=user, caller_org_id=org_id)
    mode = tenancy_mode(db)
    audit = mode is TenancyMode.SINGLE and _forced_single() and _active_orgs_exist(db)
    return PlatformBypass(user=user, mode=mode, caller_org_id=org_id, audit_cross_tenant=audit)
