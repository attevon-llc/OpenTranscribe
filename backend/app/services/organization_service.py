"""Organization (tenant) resolution helpers — cloud-edition seam.

Background/ingest paths (watch-source imports, URL downloads, storage
recovery) have no request context, so they can't read the active org from
``RequestContext``. They derive it from the owner's membership mirror instead:
the owner's first active organization, else personal scope (``None``).

In the community/self-hosted edition the membership table is empty, so this
always returns ``None`` and every file stays personal — behavior-identical to
before the cloud seam existed.
"""

import logging

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def resolve_owner_org_id(db: Session, user_id: int) -> int | None:
    """Return the owner's active organization id, or ``None`` for personal scope.

    Picks the user's first active organization membership (the membership
    mirror is the authorization source of truth — never a token). Returns
    ``None`` when the user has no org membership, which is always the case in
    the community edition.

    Args:
        db: Database session.
        user_id: Owner user id whose org should be resolved.

    Returns:
        The local ``organization.id`` of the owner's first active org, or
        ``None`` for personal scope.
    """
    from app.models.organization import Organization
    from app.models.organization import OrganizationMembership

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
