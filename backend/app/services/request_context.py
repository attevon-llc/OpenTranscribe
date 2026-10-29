"""The authenticated-request tenant context value object.

Lives in ``services`` (not ``api``) because service-layer code such as the chat scope
resolver consumes it; ``app.api.deps_context`` builds it per request.
"""

from dataclasses import dataclass

from sqlalchemy.orm import Query
from sqlalchemy.orm import Session

from app.models.user import User
from app.services.platform_bypass import PlatformBypass
from app.utils.db_helpers import get_user_files_query


@dataclass(frozen=True)
class RequestContext:
    """Authenticated user + active tenant scope for this request."""

    user: User
    org_id: int | None = None  # our organization.id (NOT the provider's string id)
    org_role: str | None = None  # "org:admin" | "org:member" | None
    # What the platform-admin role may reach on this request. Defaults to nothing, so a
    # context built by hand fails closed (issue #1122).
    bypass: PlatformBypass = PlatformBypass.none()

    @property
    def is_org_context(self) -> bool:
        return self.org_id is not None

    @property
    def is_org_admin(self) -> bool:
        return self.org_role == "org:admin"


def get_user_files_query_for_context(db: Session, ctx: RequestContext) -> Query:
    """``get_user_files_query`` using a :class:`RequestContext` for tenant scope."""
    return get_user_files_query(db, ctx.user.id, organization_id=ctx.org_id)
