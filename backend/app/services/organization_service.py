"""Organization (tenant) resolution helpers — cloud-edition seam.

**Last-resort fallback only (issue #262c).** Guessing a tenant from the
owner's memberships mis-stamps imports for multi-org users, so every ingest
path that CAN capture the org at request time now does:

  * watch-source imports use ``watch_source.organization_id`` (captured at
    source-creation time, backfilled by v372);
  * playlist/URL placeholders receive the originating request's ``ctx.org_id``
    through the task kwargs.

The only remaining legitimate caller is storage recovery / reingestion
(``storage_recovery_service``), which reconstructs rows with genuinely NO
originating context. Do not add new callers — thread the request org instead.

In the community/self-hosted edition the membership table is empty, so this
always returns ``None`` and every file stays personal — behavior-identical to
before the cloud seam existed.
"""

import logging

from sqlalchemy.orm import Session

from app.models.organization import Organization
from app.models.organization import OrganizationMembership

logger = logging.getLogger(__name__)


def resolve_owner_org_id(db: Session, user_id: int) -> int | None:
    """Return the owner's active organization id, or ``None`` for personal scope.

    LAST-RESORT fallback (see module docstring): picks the user's FIRST active
    organization membership, which is a guess for multi-org users. Only for
    paths with no originating request context (storage recovery). Returns
    ``None`` when the user has no org membership, which is always the case in
    the community edition.

    Args:
        db: Database session.
        user_id: Owner user id whose org should be resolved.

    Returns:
        The local ``organization.id`` of the owner's first active org, or
        ``None`` for personal scope.
    """
    membership = (
        db.query(OrganizationMembership)
        .join(Organization, Organization.id == OrganizationMembership.organization_id)
        .filter(
            OrganizationMembership.user_id == user_id,
            Organization.is_active.is_(True),
        )
        .order_by(OrganizationMembership.id.asc())
        .first()
    )
    if membership is None:
        return None
    return int(membership.organization_id)


def find_active_org_by_external_id(db: Session, external_org_id: str) -> Organization | None:
    """The active organization mirrored from the identity provider's ``external_org_id``."""
    return (
        db.query(Organization)
        .filter(Organization.external_org_id == external_org_id, Organization.is_active.is_(True))
        .first()
    )


def find_membership(
    db: Session, organization_id: int, user_id: int
) -> OrganizationMembership | None:
    """The user's membership row in this organization, or ``None``."""
    return (
        db.query(OrganizationMembership)
        .filter(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == user_id,
        )
        .first()
    )
