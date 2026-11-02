import logging
from datetime import UTC
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import Response
from fastapi import status
from sqlalchemy.orm import Session

from app.api.deps_context import get_current_context
from app.api.endpoints import users_admin_edit
from app.api.endpoints import users_self_service
from app.api.endpoints.auth import get_current_admin_user
from app.auth.constants import EXTERNAL_AUTH_NO_PASSWORD
from app.auth.password_history import add_password_to_history
from app.auth.rate_limit import get_directory_rate_limit
from app.auth.rate_limit import limiter
from app.auth.rate_limit import user_or_ip_key
from app.auth.roles import ROLE_USER
from app.auth.roles import VALID_ROLES
from app.auth.roles import role_implies_superuser
from app.auth.utils import local_password_allowed
from app.auth.utils import mask_email_for_display
from app.core.auth_types import AUTH_TYPE_LOCAL
from app.core.auth_types import VALID_AUTH_TYPES
from app.core.security import get_password_hash
from app.db.base import get_db
from app.models.user import User
from app.schemas.user import User as UserSchema
from app.schemas.user import UserCreate
from app.schemas.user import UserSearchResult
from app.services.request_context import RequestContext
from app.services.uuid_helpers import get_user_by_uuid

logger = logging.getLogger(__name__)

router = APIRouter()


def create_user(user_data: UserCreate, db: Session) -> User:
    """Create a new user (admin provisioning path).

    Called from ``admin.create_admin_user``. It is the direct-provisioning
    counterpart to the invitation flow (``auth/invitations.py``), which is the
    preferred path because it never has an admin choose someone else's password.

    Three gaps this used to have, all of which made "disable self-registration"
    unusable in practice (v377):

    * ``auth_type`` could not be set, so every admin-created account was
      ``local`` — unable to log in at all where local passwords are off.
    * No ``password_changed_at``, no password-history row: password expiry and
      reuse prevention (FedRAMP IA-5) both start from missing data.
    * A local account got a password the *admin* chose and no forced change, so
      the admin permanently knew a working credential for someone else's
      account. ``must_change_password`` is now set on that path.

    Args:
        user_data: Validated create payload. ``password`` is present only for
            ``auth_type == "local"`` (enforced by the schema).
        db: Database session.

    Returns:
        The created user.

    Raises:
        HTTPException: 400 if the email is taken or the role is invalid.
    """
    # Check if email already exists
    db_user = db.query(User).filter(User.email == user_data.email).first()
    if db_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Email already registered"
        )

    # role is the authorization source of truth; is_superuser is derived from it
    # (never taken from the client). Privilege of the *caller* is enforced by the
    # endpoint (see admin.create_admin_user); this helper only validates the value.
    role = user_data.role or ROLE_USER
    if role not in VALID_ROLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid role: {role}",
        )

    auth_type = user_data.auth_type or AUTH_TYPE_LOCAL
    if auth_type not in VALID_AUTH_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid auth_type: {auth_type}",
        )

    # local_password_allowed is the single source of truth for whether an account
    # may hold a local password. A freshly created account never has
    # allow_local_fallback, so pki/oidc/ldap all land on the placeholder.
    holds_local_password, _reason = local_password_allowed(auth_type, False)
    now = datetime.now(UTC)

    if holds_local_password:
        if not user_data.password:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Password is required for local accounts",
            )
        password_hash = get_password_hash(user_data.password)
    else:
        password_hash = EXTERNAL_AUTH_NO_PASSWORD

    new_user = User(
        email=user_data.email,
        hashed_password=password_hash,
        full_name=user_data.full_name,
        is_active=user_data.is_active if user_data.is_active is not None else True,
        role=role,
        is_superuser=role_implies_superuser(role),
        auth_type=auth_type,
        password_changed_at=now if holds_local_password else None,
        # The admin knows this password. It must not stay the account's password.
        must_change_password=holds_local_password,
    )

    db.add(new_user)
    db.flush()

    if holds_local_password:
        # Without this row the initial password is invisible to reuse checks.
        add_password_to_history(db, int(new_user.id), password_hash)

    db.commit()
    db.refresh(new_user)

    return new_user


@router.get("", response_model=list[UserSchema])
def list_users(
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> Any:
    """
    List users (admin only) with optional pagination.
    """
    users = db.query(User).order_by(User.id).offset(offset).limit(limit).all()
    return users


router.include_router(users_self_service.router)


@router.get("/search", response_model=list[UserSearchResult])
@limiter.limit(get_directory_rate_limit(), key_func=user_or_ip_key)
def search_users(
    *,
    request: Request,
    q: str = Query(..., min_length=2, max_length=100, description="Search query (min 2 chars)"),
    response: Response = None,  # type: ignore[assignment]  # required by slowapi
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> Any:
    """Search users by name or email for sharing autocomplete.

    Returns up to 20 results, excluding the caller.

    **Tenant-gated.** This is the only plain-``user``-tier route that reads other
    accounts' identities, and it carried no tenant scope at all — not even the
    ``UNSCOPED`` sentinel, because no ``RequestContext`` reached it. In an org
    deployment that made it a cross-tenant directory: any authenticated member of
    any tenant could page every *other* tenant's active accounts — email plus
    full name — out of it, 20 at a time, from a two-character query, with no
    admin privilege and nothing in the response to say the rows came from
    somewhere else.

    The gate mirrors ``scope_to_context``: in org context only members of THAT
    org are candidates; in personal scope only accounts with no org membership
    are. Community-edition invariance holds exactly — the membership table is
    empty there, so every account is in personal scope and the result set is
    unchanged.

    **Rate-limited** (issue #904), keyed per-user: ``RATE_LIMIT_DIRECTORY_PER_MINUTE``
    (default 60/minute). This closes the request-VOLUME gap this route had — an
    authenticated user could otherwise page the whole tenant directory as fast as the
    connection allowed. It is a volume/noise bound, not a proof against enumeration:
    the two-character minimum and the tenant gate above are what limit what any single
    request can see.

    **Payload-minimized** (issue #904): the response never carries a full email
    address. ``UserSearchResult.masked_email`` is a display-only mask
    (``mask_email_for_display``) — even an exhaustive scrape at the allowed rate
    cannot recover a real address from it.
    """
    from sqlalchemy import exists
    from sqlalchemy import or_

    from app.models.organization import OrganizationMembership

    # Correlated EXISTS against the outer `user` row, so the tenant gate is one
    # subquery rather than a join that could duplicate rows per membership.
    caller_org_member = exists().where(
        OrganizationMembership.user_id == User.id,
        OrganizationMembership.organization_id == ctx.org_id,
    )
    any_org_member = exists().where(OrganizationMembership.user_id == User.id)
    tenant_gate = caller_org_member if ctx.is_org_context else ~any_org_member

    pattern = f"%{q}%"
    users = (
        db.query(User)
        .filter(
            User.id != ctx.user.id,
            User.is_active == True,  # noqa: E712
            tenant_gate,
            or_(
                User.email.ilike(pattern),
                User.full_name.ilike(pattern),
            ),
        )
        .order_by(User.full_name, User.email)
        .limit(20)
        .all()
    )

    return [
        UserSearchResult(
            uuid=u.uuid, full_name=u.full_name, masked_email=mask_email_for_display(u.email)
        )
        for u in users
    ]


@router.get("/{user_uuid}", response_model=UserSchema)
def get_user(
    user_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> Any:
    """
    Get user by UUID (admin only)
    """
    # Uses helper that validates UUID format and returns 400 for invalid UUIDs
    return get_user_by_uuid(db, user_uuid)


router.include_router(users_admin_edit.router)
