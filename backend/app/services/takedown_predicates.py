"""Takedown review reach: whether the platform role may read a quarantined file.

Split out of ``takedown_service`` (issue #1253). Nothing here mutates takedown state, and nothing
here is a read rule: every read surface asks ``app.services.file_availability`` (the one
availability rule, quarantine AND trash), which builds on the two answers below.

  * :func:`quarantine_exempt` - the platform role reached this row (``bypass.allows`` against the
    row's own tenant).
  * :func:`is_review_admin` - the same question for the async plane, which holds a user id and
    has no request (and therefore no support grant).

Visibility is **platform-admin only**, including for the organization admin who placed an
organization takedown: an org admin reads a quarantined file exactly as its owner does (not at
all) and manages it from metadata (issue #1253).
"""

import logging
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.models.media import MediaFile
from app.models.user import User

if TYPE_CHECKING:
    from app.services.platform_bypass import PlatformBypass

logger = logging.getLogger(__name__)


def quarantine_exempt(bypass: "PlatformBypass", file: MediaFile) -> bool:
    """The ONE quarantine read rule: exempt only where the platform role reached this row.

    ``bypass.allows`` decides against the row's own tenant (single mode: any admin;
    multi-tenant mode: only under a support-access grant covering that tenant). A bare
    admin role (``bypass.user_is_admin``) is not the test: it says nothing about whether
    the platform role may act on this row.

    Args:
        bypass: The request's platform bypass.
        file: The loaded media file.

    Returns:
        True if the platform role may read ``file`` for review.
    """
    return bypass.allows(
        org_id=file.organization_id,
        owner_id=file.user_id,
        need="read",
        resource_type="media_file",
        resource_uuid=str(file.uuid),
    )


def is_review_admin(db: Session, user_id: int | None) -> bool:
    """Whether user_id holds the review visibility every predicate here is relative to.

    Public because the ASYNC plane needs it: a Celery task holds a user id, never a
    User, and must re-resolve the role at RUN time rather than trust a flag captured
    at dispatch. None is never an admin.

    A background task has no request, so no support-access grant can reach it. The
    only deployment where an admin's role alone reaches every row is single-tenant mode
    (the same rule as ``PlatformBypass.allows`` there); in multi-tenant mode this is
    False for everyone, which keeps the async plane fail-closed.
    """
    if user_id is None:
        return False
    from app.services.platform_access import TenancyMode
    from app.services.platform_access import tenancy_mode

    recipient = db.query(User).filter(User.id == int(user_id)).first()
    return bool(recipient and recipient.is_admin and tenancy_mode(db) is TenancyMode.SINGLE)
