"""Account-lifecycle gates applied by ``get_current_active_user`` (FedRAMP AC-2 / IA-5 / AC-8).

Approval, expiry, login-banner acknowledgment and forced password change, plus the
audit/client-attribution helpers they share. ``dependencies.py`` imports every gate
into its own globals and calls them from there.
"""

import logging
from datetime import UTC
from datetime import datetime

from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session

from app.auth.audit import audit_logger
from app.auth.audit_events import AuditEventType
from app.auth.audit_events import AuditOutcome
from app.core.config import settings
from app.models.user import User

logger = logging.getLogger(__name__)


# ── account-lifecycle gate (FedRAMP AC-2 / IA-5) ────────────────────────────────
#
# ``must_change_password`` and ``account_expires_at`` were both written and never
# read: the admin "force change on next login" flag let the user sign in with the
# admin-chosen password and never prompted, and a time-boxed contractor account
# stayed usable forever. Both are enforced here, at the one dependency every
# user-facing route passes through.

#: Machine-readable codes carried in the 403 ``detail``. The SPA branches on
#: ``detail.code`` to render the forced-change screen; prose is not a contract,
#: and a client that has to string-match an English message breaks on rewording.
ERROR_CODE_PASSWORD_CHANGE_REQUIRED = "password_change_required"  # noqa: S105 # nosec B105
ERROR_CODE_ACCOUNT_EXPIRED = "account_expired"
ERROR_CODE_BANNER_ACKNOWLEDGMENT_REQUIRED = "banner_acknowledgment_required"
#: Account exists and its credential worked, but an administrator has not admitted
#: it yet (``v381``; see ``app/auth/approval.py``). Its own code rather than a reuse
#: of ``account_expired`` because the remedy is somebody else's action, not the
#: user's, and the SPA has to say so.
ERROR_CODE_ACCOUNT_PENDING_APPROVAL = "account_pending_approval"
#: An administrator refused the account. Distinct from the code above because
#: telling a rejected applicant they are "pending" is simply false, and because the
#: two are enforced under different conditions — see :func:`_enforce_approval`.
ERROR_CODE_ACCOUNT_REJECTED = "account_rejected"

#: Routes that stay reachable while ``must_change_password`` is set: the endpoint
#: that CLEARS the flag (``PUT /users/me`` — the self-service password change; its
#: GET twin is the caller's own profile, which the change screen renders), plus the
#: routes that end the session. Logout does not resolve through this dependency
#: today; it is listed so the exemption survives a refactor that gives it one.
#: Matched against the resolved route template, so a path parameter can never be
#: crafted to look like an exempt path.
PASSWORD_CHANGE_EXEMPT_PATHS = frozenset(
    {
        f"{settings.API_PREFIX}/users/me",
        f"{settings.API_PREFIX}/auth/logout",
        f"{settings.API_PREFIX}/auth/logout/all",
    }
)

#: Routes that stay reachable while the login banner is unacknowledged: the
#: endpoint that RECORDS the acknowledgment (without it the user can never clear
#: the gate), the banner text itself, and the routes that end the session. Same
#: route-template matching as above, for the same reason.
BANNER_EXEMPT_PATHS = frozenset(
    {
        f"{settings.API_PREFIX}/auth/banner",
        f"{settings.API_PREFIX}/auth/banner/acknowledge",
        f"{settings.API_PREFIX}/auth/logout",
        f"{settings.API_PREFIX}/auth/logout/all",
    }
)


def _route_path(request: Request | None) -> str:
    """Return the matched route template for *request*.

    Falls back to the raw URL path when no route has been matched yet (and to
    an empty string for a request stand-in), which fails safe: an unknown path
    is not in the exempt set, so the caller is refused.
    """
    scope = getattr(request, "scope", None)
    route = scope.get("route") if isinstance(scope, dict) else None
    path = getattr(route, "path", None) or getattr(getattr(request, "url", None), "path", "") or ""
    return path.rstrip("/") if len(path) > 1 else path


def _lifecycle_client_info(request: Request | None) -> tuple[str, str]:
    """Client IP / user agent for a lifecycle audit event, never raising.

    An audit record must not be the reason an authorization decision fails, so
    an unusual request object degrades to "unknown" instead of a 500.
    """
    try:
        return _get_client_info(request)  # type: ignore[arg-type]
    except Exception:
        logger.debug("Could not resolve client info for lifecycle audit", exc_info=True)
        return "unknown", "unknown"


def _audit_lifecycle_denial(
    event_type: AuditEventType,
    user: User,
    request: Request | None,
    error_code: str,
    details: dict,
) -> None:
    """Record an account-lifecycle access denial (FedRAMP AU-2)."""
    client_ip, user_agent = _lifecycle_client_info(request)
    audit_logger.log(
        event_type=event_type,
        outcome=AuditOutcome.FAILURE,
        user_id=getattr(user, "id", None),
        username=str(getattr(user, "email", "") or ""),
        source_ip=client_ip,
        user_agent=user_agent,
        error_code=error_code,
        details=details,
    )


def _expired_at(user: User) -> datetime | None:
    """``account_expires_at`` (timezone-aware) when it has passed, else ``None``."""
    expires_at = getattr(user, "account_expires_at", None)
    if expires_at is None:
        return None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at if datetime.now(UTC) >= expires_at else None


def _enforce_account_expiry(user: User, request: Request | None) -> None:
    """Refuse a time-boxed account past its ``account_expires_at``.

    Unconditional — unlike a forced password change there is no self-service
    remedy, so no route is exempt.

    Raises:
        HTTPException: 403 with ``detail.code == account_expired``.
    """
    expires_at = _expired_at(user)
    if expires_at is None:
        return

    _audit_lifecycle_denial(
        AuditEventType.AUTH_ACCOUNT_EXPIRED,
        user,
        request,
        error_code="ACCOUNT_EXPIRED",
        details={
            "trigger": "fixed_date",
            "expired_at": expires_at.isoformat(),
            "path": _route_path(request),
        },
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": ERROR_CODE_ACCOUNT_EXPIRED,
            "message": (
                f"This account expired on {expires_at.date().isoformat()}. "
                "Contact an administrator to extend it."
            ),
        },
    )


def _approval_refusal(user: User, db: Session) -> tuple[str, str] | None:
    """``(detail.code, message)`` when the approval gate refuses *user*, else ``None``.

    An ``approval_status`` outside the closed set is refused as a rejection: it has
    no self-service remedy either.

    Raises:
        HTTPException: 503 ``auth_config_unavailable`` for a pending account when the
            setting cannot be read.
    """
    from app.api.endpoints.auth.approval_gate import approval_enforced
    from app.auth.approval import is_pending
    from app.auth.approval import is_rejected
    from app.auth.approval import is_unrecognised

    if is_rejected(user) or is_unrecognised(user):
        return (
            ERROR_CODE_ACCOUNT_REJECTED,
            "This account was not approved. Contact an administrator.",
        )
    if is_pending(user) and approval_enforced(db):
        return (
            ERROR_CODE_ACCOUNT_PENDING_APPROVAL,
            "This account is awaiting administrator approval.",
        )
    return None


def admitted_on_optional_auth(user: User, db: Session) -> bool:
    """Whether an optional-auth route may treat *user* as signed in.

    Applies only the approval and expiry gates, without raising or auditing: the
    caller is served as anonymous rather than refused, and these routes run on
    every page load. The banner and password-change gates are left out because
    both have a self-service remedy whose screens run in that state.
    """
    if _expired_at(user) is not None:
        return False
    try:
        return _approval_refusal(user, db) is None
    except HTTPException:
        return False


def _enforce_approval(user: User, request: Request | None, db: Session) -> None:
    """Refuse an account an administrator has not admitted (or has refused).

    The two states are enforced under deliberately different conditions:

    * ``pending`` only bites while ``require_account_approval`` is on. Turning the
      setting off is the operator's escape hatch — it releases a queue they no
      longer want to work through — and leaving pending accounts stranded after
      the policy was withdrawn would make the control hard to reverse.
    * ``rejected`` bites unconditionally. It is a decision about one account, like
      deactivation, not a policy that can be switched off underneath it.

    No route is exempt: unlike a forced password change there is nothing the user
    can do, so routing them anywhere would only reach a screen that cannot help.

    Args:
        user: The authenticated account.
        request: Used for the audit record's client attribution.
        db: Session used to resolve the setting (already open for this request).

    Raises:
        HTTPException: 403 with ``detail.code`` ``account_pending_approval`` or
            ``account_rejected``; 503 ``auth_config_unavailable`` for a pending
            account when the setting cannot be read.
    """
    refusal = _approval_refusal(user, db)
    if refusal is None:
        return
    code, message = refusal

    # Audited, unlike the banner gate: it fires only for individually flagged
    # accounts, so volume is bounded, and an operator reviewing the queue wants it.
    _audit_lifecycle_denial(
        AuditEventType.AUTH_ACCOUNT_DISABLED,
        user,
        request,
        error_code=code.upper(),
        details={
            "approval_status": str(getattr(user, "approval_status", "")),
            "path": _route_path(request),
        },
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": code, "message": message},
    )


def _enforce_password_change(user: User, request: Request | None) -> None:
    """Confine a caller flagged ``must_change_password`` to the change endpoint.

    Raises:
        HTTPException: 403 with ``detail.code == password_change_required``.
    """
    if not getattr(user, "must_change_password", False):
        return

    path = _route_path(request)
    if path in PASSWORD_CHANGE_EXEMPT_PATHS:
        return

    _audit_lifecycle_denial(
        AuditEventType.AUTH_PASSWORD_EXPIRED,
        user,
        request,
        error_code="PASSWORD_CHANGE_REQUIRED",
        details={"reason": "must_change_password", "path": path},
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": ERROR_CODE_PASSWORD_CHANGE_REQUIRED,
            "message": "You must change your password before continuing.",
        },
    )


def _banner_requirement(db: Session | None) -> tuple[bool, datetime | None]:
    """Return ``(banner_enabled, text_last_changed_at)`` for this deployment.

    Reads the two ``auth_config`` rows in one query — the same DB-over-.env
    precedence the banner endpoints use, so a banner enabled in the admin UI is
    enforced, not just displayed.

    ``text_last_changed_at`` is the ``login_banner_text`` row's ``updated_at``,
    which is what makes an acknowledgment expire when the wording changes; see
    :func:`_enforce_banner_acknowledgment`. It is ``None`` when the text comes
    from ``.env`` (no row, hence no change history — and an ``.env`` edit needs a
    restart anyway).

    Args:
        db: Database session, or anything without a ``query`` when none is
            available. Falls back to the environment values rather than raising:
            this runs on every authenticated request.

    Returns:
        Whether the banner is enabled, and when its text last changed.
    """
    if db is None or not hasattr(db, "query"):
        return settings.LOGIN_BANNER_ENABLED, None

    try:
        from app.models.auth_config import AuthConfig

        rows = {
            row.config_key: row
            for row in db.query(AuthConfig)
            .filter(AuthConfig.config_key.in_(("login_banner_enabled", "login_banner_text")))
            .all()
        }
    except Exception:
        logger.debug("Could not read banner configuration; using .env", exc_info=True)
        return settings.LOGIN_BANNER_ENABLED, None

    enabled_row = rows.get("login_banner_enabled")
    if enabled_row is None or enabled_row.config_value is None:
        enabled = settings.LOGIN_BANNER_ENABLED
    else:
        from app.services.auth_config_service import BOOL_TRUE_VALUES

        enabled = str(enabled_row.config_value).strip().lower() in BOOL_TRUE_VALUES

    text_row = rows.get("login_banner_text")
    return enabled, getattr(text_row, "updated_at", None)


def _enforce_banner_acknowledgment(user: User, request: Request | None, db: Session) -> None:
    """Confine a caller who has not accepted the login banner (FedRAMP AC-8).

    ``banner_acknowledged_at`` was written by ``POST /auth/banner/acknowledge``
    — whose own docstring says it "must be called after login before granting
    full access" — and read by **nothing**. The SPA approximated the control with
    a ``sessionStorage`` flag, which clears per tab, is trivially removed, and
    never reaches the server at all, so the consent AC-8 requires was never
    actually a precondition for anything.

    An acknowledgment **expires when the banner text changes.** A user who
    accepted "UNCLASSIFIED — monitoring in effect" has not accepted a later
    "SECRET — no personal use"; treating the old click as consent to new wording
    is precisely the thing the control exists to prevent, and an admin editing
    the banner is a deliberate act with an obvious expectation. The comparison is
    against the ``login_banner_text`` row's ``updated_at``, so it costs no schema
    and no extra query.

    Raises:
        HTTPException: 403 with ``detail.code == banner_acknowledgment_required``.
    """
    enabled, text_changed_at = _banner_requirement(db)
    if not enabled:
        return

    path = _route_path(request)
    if path in BANNER_EXEMPT_PATHS:
        return

    acknowledged_at = getattr(user, "banner_acknowledged_at", None)
    if acknowledged_at is not None:
        if acknowledged_at.tzinfo is None:
            acknowledged_at = acknowledged_at.replace(tzinfo=UTC)
        changed_at = text_changed_at
        if changed_at is not None and changed_at.tzinfo is None:
            changed_at = changed_at.replace(tzinfo=UTC)
        if changed_at is None or acknowledged_at >= changed_at:
            return
        reason = "banner_text_changed"
    else:
        reason = "never_acknowledged"

    # Deliberately NOT audited. Unlike the two gates above — which fire for one
    # flagged account at a time — this refuses every request of every session
    # until the user clicks through, so per-request events would swamp the
    # OpenSearch audit index. The AC-8 artefact is the acknowledgment itself,
    # which `POST /auth/banner/acknowledge` already records.
    logger.debug("Banner acknowledgment required for user %s (%s)", user.id, reason)
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": ERROR_CODE_BANNER_ACKNOWLEDGMENT_REQUIRED,
            "message": "You must acknowledge the login banner before continuing.",
            "reason": reason,
        },
    )


def _get_client_info(request: Request | None) -> tuple[str, str]:
    """Extract client IP and user agent from request.

    Args:
        request: FastAPI request object, or ``None`` where none is in scope.

    Returns:
        Tuple of (client_ip, user_agent); ``("unknown", "unknown")`` when *request* is
        ``None``. Callers audit from service-layer and background paths that genuinely
        have no request, and an audit record that cannot resolve an address must never
        turn the operation it is recording into a 500.
    """
    if request is None:
        return "unknown", "unknown"

    # Resolve through the trusted-proxy chain, not the raw peer: behind a reverse proxy
    # request.client.host is the PROXY, so every audited login recorded the proxy's
    # address instead of the user's (issue #284 A0.5).
    from app.utils.client_ip import resolve_client_ip

    client_ip = resolve_client_ip(request)
    user_agent = request.headers.get("User-Agent", "unknown")
    return client_ip, user_agent
