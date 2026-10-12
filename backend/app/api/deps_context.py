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
from dataclasses import replace
from typing import Any

from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Query
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_active_user
from app.core.constants import SUPPORT_ACCESS_GRANT_HEADER
from app.core.route_template import route_label
from app.core.tenancy import UNSCOPED  # noqa: F401 — re-exported for callers
from app.core.tenancy import OrgScope  # noqa: F401 — re-exported for callers
from app.core.tenancy import _Unscoped  # noqa: F401 — re-exported for callers
from app.db.base import get_db
from app.models.user import User
from app.services.platform_access import TenancyMode
from app.services.platform_access import tenancy_mode
from app.services.platform_bypass import PlatformBypass
from app.services.platform_bypass import build_bypass
from app.services.support_access_service import coded_error
from app.services.support_access_service import record_use
from app.services.support_access_service import resolve_active_grant

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

logger = logging.getLogger(__name__)


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


def get_base_context(
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

    The base context never reads a support-access grant header, so the lifecycle
    routes (and ``require_org_admin``) cannot be broken by a stale one.
    """
    org_id, org_role = resolve_org_context(request, db, current_user)
    return RequestContext(
        user=current_user,
        org_id=org_id,
        org_role=org_role,
        bypass=build_bypass(db, current_user, org_id),
    )


def resolve_request_bypass(
    request: Request,
    db: Session,
    user: User,
    org_id: int | None,
    org_role: str | None,
) -> tuple[int | None, str | None, PlatformBypass]:
    """Resolve ``(org_id, org_role, bypass)``, honouring ``X-Support-Access-Grant``.

    The ONE implementation, shared by ``get_current_context`` and the optional-auth
    thumbnail route so the grant rules cannot diverge between them. Without the header the
    request has no grant, ever: a grant is never ambient. With it:

    1. multi-tenant mode only (else 400 ``support_access_unavailable``);
    2. the grant must be the caller's own, active, with a live target (coded 403);
    3. a ``read`` grant refuses every non-safe method (403 ``support_grant_write_required``),
       even on routes that never ask the bypass anything;
    4. an ORG grant makes the request *assume* that tenant with ``org_role=None`` (so a grant
       never confers ``org:admin``); a personal grant leaves the caller's scope alone;
    5. the request-level use row is written before the handler runs, fail-closed (503).
    """
    header = (request.headers.get(SUPPORT_ACCESS_GRANT_HEADER) or "").strip()
    if not header:
        return org_id, org_role, build_bypass(db, user, org_id)
    if tenancy_mode(db) is not TenancyMode.MULTI:
        raise coded_error(
            status.HTTP_400_BAD_REQUEST,
            "support_access_unavailable",
            "Support access is not available on this deployment.",
        )
    grant = resolve_active_grant(db, header, user)
    if request.method not in SAFE_METHODS and grant.access_level != "write":
        raise coded_error(
            status.HTTP_403_FORBIDDEN,
            "support_grant_write_required",
            "This support access grant is read-only.",
        )
    if grant.is_org_grant:
        org_id, org_role = grant.organization_id, None
        request.state.org_id = org_id
    route = route_label(getattr(request.scope.get("route"), "path", None))
    record_use(
        grant,
        actor_user_id=int(user.id),
        method=request.method,
        route=route,
        resource_type=None,
        resource_uuid=None,
        need=None,
        organization_id=grant.organization_id,
        owner_user_id=grant.subject_user_id,
    )
    bypass = build_bypass(db, user, org_id, grant=grant, method=request.method, route=route)
    return org_id, org_role, bypass


def get_current_context(
    request: Request,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_base_context),
) -> RequestContext:
    """The context content routes use: the base context plus any support-access grant."""
    if not request.headers.get(SUPPORT_ACCESS_GRANT_HEADER, "").strip():
        return ctx
    org_id, org_role, bypass = resolve_request_bypass(
        request, db, ctx.user, ctx.org_id, ctx.org_role
    )
    return replace(ctx, org_id=org_id, org_role=org_role, bypass=bypass)


def refuse_under_support_grant(
    ctx: RequestContext = Depends(get_current_context),
) -> RequestContext:
    """403 when the request carries a support-access grant (plan rules 8-9).

    A grant is for in-product diagnosis of one tenant. It never creates tenant content,
    never acts as an org admin, never feeds an LLM, and never exports. Attach this at router
    level to the surfaces that must refuse it; the refused attempt is still recorded, because
    the request-level use row was written while the context resolved.
    """
    if ctx.bypass.under_grant:
        raise coded_error(
            status.HTTP_403_FORBIDDEN,
            "support_grant_action_not_permitted",
            "This action is not available under a support access grant.",
        )
    return ctx


def require_org_admin(
    ctx: RequestContext = Depends(get_base_context),
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
