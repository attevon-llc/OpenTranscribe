"""Request context with tenant (organization) scope — cloud-edition seam.

``get_current_context`` wraps ``get_current_user`` and resolves the active
organization for this request:
  - community / personal: no org -> personal scope (user_id filtering)
  - cloud: the external verifier stashed the verified identity (with the
    provider org id) on ``request.state``; we map it to our Organization row
    and authorize against the **membership mirror**, never the token alone,
    so a removed member loses access before their token expires.

``scope_to_context`` is the default-deny query helper every org-aware
endpoint uses: org context -> filter by organization_id, else by user_id.
"""

import logging
from dataclasses import dataclass
from typing import Any

from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Query
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_active_user
from app.core.tenancy import UNSCOPED  # noqa: F401 — re-exported for callers
from app.core.tenancy import OrgScope  # noqa: F401 — re-exported for callers
from app.core.tenancy import _Unscoped  # noqa: F401 — re-exported for callers
from app.db.base import get_db
from app.models.user import User

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RequestContext:
    """Authenticated user + active tenant scope for this request."""

    user: User
    org_id: int | None = None  # our organization.id (NOT the provider's string id)
    org_role: str | None = None  # "org:admin" | "org:member" | None

    @property
    def is_org_context(self) -> bool:
        return self.org_id is not None

    @property
    def is_org_admin(self) -> bool:
        return self.org_role == "org:admin"


def resolve_org_context(request: Request, db: Session, user: User) -> tuple[int | None, str | None]:
    """Resolve ``(org_id, org_role)`` for ``user`` on this request (None = personal).

    The SINGLE source of org-scope resolution, reused by ``get_current_context``
    AND by optional-auth routes (e.g. the thumbnail) so the membership-mirror
    authorization rule is never reimplemented divergently. Authorization follows
    the mirror, not the token alone: a removed member resolves to personal scope.
    """
    identity = getattr(request.state, "external_identity", None)
    if identity is None or not getattr(identity, "org_id", None):
        return None, None

    from app.models.organization import Organization
    from app.models.organization import OrganizationMembership

    org = (
        db.query(Organization)
        .filter(Organization.external_org_id == identity.org_id, Organization.is_active.is_(True))
        .first()
    )
    if org is None:
        # Org not mirrored (webhook lag) — fall back to personal scope rather
        # than failing the request; the next webhook/login sync repairs it.
        logger.warning(f"Unknown org in token: {identity.org_id} (falling back to personal)")
        return None, None

    membership = (
        db.query(OrganizationMembership)
        .filter(
            OrganizationMembership.organization_id == org.id,
            OrganizationMembership.user_id == user.id,
        )
        .first()
    )
    if membership is None:
        # Token claims an org the mirror doesn't confirm (e.g. member just
        # removed). Authorization follows the mirror: personal scope only.
        logger.warning(
            f"User {user.id} carries org {identity.org_id} in token "
            "but has no membership row — personal scope applied"
        )
        return None, None

    # Cloud contract: refine the access-log org_id from the provider's raw
    # string (stashed by get_current_user) to OUR local Organization.id, now
    # that org context is confirmed against the membership mirror. Access log
    # only — never a Prometheus label. Cloud inherits this on submodule bump.
    request.state.org_id = org.id
    return org.id, membership.role


def get_current_context(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> RequestContext:
    """Resolve the request's tenant context (personal when no org applies).

    Chains through ``get_current_active_user``, not ``get_current_user``: this is
    the credential entry point for ~100 routes (all of chat, org-admin, tags,
    collections, upload prepare/cancel), and depending on the credential layer
    meant every one of them silently opted out of the account-lifecycle gate —
    a deactivated, expired, unapproved or ``must_change_password`` account could
    still create conversations, delete files and read an org's audit log.
    """
    org_id, org_role = resolve_org_context(request, db, current_user)
    return RequestContext(user=current_user, org_id=org_id, org_role=org_role)


def require_org_admin(
    ctx: RequestContext = Depends(get_current_context),
) -> RequestContext:
    """FastAPI dependency: 403 unless the caller is an admin of an active org.

    The **server-side** authority for every org-admin action (team/member
    management, org-level settings/policies, org audit-log read, org GDPR
    erasure). The frontend ``audience == "org_admin"`` capability gate is
    cosmetic only — it decides what UI renders, never who is authorized. Any
    mutating org-admin endpoint MUST depend on this so the membership-mirror
    role (``ctx.org_role == "org:admin"``, resolved in ``resolve_org_context``)
    is enforced rather than trusted from the token or the frontend.

    Community-edition invariance: with no orgs, ``ctx.is_org_admin`` is always
    False and ``ctx.org_id`` is None, so this dependency 403s — org-admin
    surfaces simply don't exist in the self-host/community edition (they are
    cloud-only and never wired into a route a community user can reach).

    Returns the context unchanged on success so the endpoint can reuse
    ``ctx.org_id`` for scoping.
    """
    if not ctx.is_org_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Organization administrator access required",
        )
    return ctx


def scope_to_context(query: Query, model: Any, ctx: RequestContext) -> Query:
    """Default-deny tenancy filter: org scope when active, else personal.

    Personal scope = the user's PERSONAL rows (``organization_id IS NULL``):
    a file uploaded into an org belongs to the org space, not the uploader's
    personal space. In the community edition ``organization_id`` is always
    NULL, so this is behavior-identical to plain user_id filtering there.

    ``model`` must expose ``organization_id`` and ``user_id`` columns.
    """
    if ctx.is_org_context:
        return query.filter(model.organization_id == ctx.org_id)
    return query.filter(model.user_id == ctx.user.id, model.organization_id.is_(None))
