"""The authenticated-request tenant context value object.

Lives in ``services`` (not ``api``) because service-layer code such as the chat scope
resolver consumes it; ``app.api.deps_context`` builds it per request.
"""

from dataclasses import dataclass

from app.models.user import User
from app.services.platform_bypass import PlatformBypass


@dataclass(frozen=True)
class RequestContext:
    """Authenticated user + active tenant scope for this request."""

    user: User
    org_id: int | None = None  # our organization.id (NOT the provider's string id)
    org_role: str | None = None  # "org:admin" | "org:member" | None
    # The organization's owner (v468), loaded only for an org admin: the one who may schedule the
    # organization's erasure. None = no owner, or not loaded; both fail closed.
    org_owner_user_id: int | None = None
    # What the platform-admin role may reach on this request. Defaults to nothing, so a
    # context built by hand fails closed (issue #1122).
    bypass: PlatformBypass = PlatformBypass.none()

    @property
    def is_org_context(self) -> bool:
        return self.org_id is not None

    @property
    def is_org_admin(self) -> bool:
        return self.org_role == "org:admin"

    @property
    def is_org_owner(self) -> bool:
        return (
            self.is_org_admin
            and self.org_owner_user_id is not None
            and self.org_owner_user_id == self.user.id
        )
