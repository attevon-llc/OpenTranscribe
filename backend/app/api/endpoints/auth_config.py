"""API endpoints for authentication configuration management.

This module provides REST API endpoints for super admin users to manage
authentication configuration settings including LDAP, OIDC, PKI,
MFA, password policy, and session configurations.

All endpoints require super_admin role and include audit logging for
compliance requirements (FedRAMP, NIST 800-53).
"""

import logging
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_active_superuser
from app.api.endpoints.auth.dependencies import _get_client_info
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.db.base import get_db
from app.models.auth_config import AuthConfig
from app.models.user import User
from app.schemas.auth_config import AuthConfigAuditResponse
from app.schemas.auth_config import AuthConfigResponse
from app.schemas.auth_config import AuthConfigStatusResponse
from app.schemas.auth_config import AuthMethodTestResponse
from app.services.auth_config_service import MAX_AUDIT_LOG_LIMIT
from app.services.auth_config_service import AuthConfigService

router = APIRouter()
logger = logging.getLogger(__name__)


# The super_admin gate lives in api/endpoints/auth/dependencies.py, next to
# get_current_user and get_current_admin_user. It used to be re-declared here and
# in admin.py, each comparing against its own "super_admin" string literal rather
# than roles.ROLE_SUPER_ADMIN — three copies of one authorization rule.
get_current_super_admin_user = get_current_active_superuser


#: The category allow-list, derived from the per-category schemas so it cannot
#: drift. It used to be a literal list repeated in three route bodies, and the
#: audit route — the one that leaks data when it is wrong — had no copy at all.
VALID_CATEGORIES: tuple[str, ...] = tuple(AuthConfigService.CONFIG_CATEGORIES)


def _require_valid_category(category: str) -> None:
    """Reject a category outside the allow-list.

    Args:
        category: Category from the request path.

    Raises:
        HTTPException: 400 when the category is unknown.
    """
    if category not in VALID_CATEGORIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid category. Must be one of: {', '.join(VALID_CATEGORIES)}",
        )


def _change_summary(results: dict[str, AuthConfig]) -> dict[str, Any]:
    """Describe what a category write changed, without carrying any secret VALUE.

    A key is treated as sensitive when EITHER its stored ``is_sensitive`` flag or
    the service's ``SENSITIVE_KEYS`` set says so — fail closed, so a row written
    before a key joined that set still cannot leak its value here. Sensitive keys
    contribute their NAME only: "the OIDC client secret was rotated, by this
    person, from this address" is the auditable fact; the secret itself is
    deliberately unobtainable from every other surface of this API
    (``config_value`` is ``None`` for a sensitive key and ``is_set`` carries the
    signal), and the audit log must not become the one place it appears.

    Args:
        results: The ``AuthConfig`` rows ``bulk_update_category`` actually wrote —
            not the request payload, which may name sensitive keys whose
            "leave the stored one alone" sentinel was skipped.

    Returns:
        Details fragment: every changed key, the new values of the non-sensitive
        ones, and the names of the sensitive ones.
    """
    changes: dict[str, Any] = {}
    sensitive_changed: list[str] = []

    for key, config in results.items():
        if config.is_sensitive or key in AuthConfigService.SENSITIVE_KEYS:
            sensitive_changed.append(key)
        else:
            changes[key] = config.config_value

    return {
        "changed_keys": sorted(results),
        "changes": changes,
        "sensitive_keys_changed": sorted(sensitive_changed),
    }


def _audit_auth_config_event(
    request: Request,
    actor_id: int,
    actor_email: str,
    *,
    action: str,
    outcome: AuditOutcome,
    details: dict[str, Any],
    error_code: str | None = None,
) -> None:
    """Mirror an authentication-configuration change into the central audit log.

    Every write here also writes an ``auth_config_audit`` row, but that table is
    readable only through ``GET /api/auth-config/audit/{category}`` — one category
    at a time, no time range, no export. It is invisible to
    ``GET /admin/audit-logs``, the export endpoint, the CEF/SIEM stream and the
    org-admin view, so the configuration deciding *who may authenticate at all*
    was the one change class that never reached the compliance record
    (FedRAMP AU-2/AU-3/AU-12, GDPR Art. 30).

    ``ADMIN_SETTINGS_CHANGE`` is the established member for "an administrator
    changed deployment configuration" (SCIM tokens, group mappings, chat and mail
    settings all use it); ``details["action"]`` discriminates the surface.

    Args:
        request: Live request — the ONLY source of the client address and agent.
        actor_id: The acting super_admin's id, **snapshotted before any DB work**.
            The refusal paths audit after ``db.rollback()``, which expires every
            ORM instance in the session — reading ``current_user.id`` there costs
            a re-SELECT at best and raises ``ObjectDeletedError`` at worst.
        actor_email: The acting super_admin's email, snapshotted likewise.
            ``user_id``/``username`` name the ACTOR here; auth config has no
            target user.
        action: Surface discriminator, e.g. ``auth_config_update``.
        outcome: Whether the change was applied.
        details: Event-specific fields, merged after ``action``.
        error_code: Set on a refusal so a failed change is greppable.
    """
    client_ip, user_agent = _get_client_info(request)
    audit_logger.log(
        event_type=AuditEventType.ADMIN_SETTINGS_CHANGE,
        outcome=outcome,
        user_id=actor_id,
        username=actor_email,
        source_ip=client_ip,
        user_agent=user_agent,
        error_code=error_code,
        details={"action": action, **details},
    )


@router.get("", response_model=dict[str, list[AuthConfigResponse]])
def get_all_configs(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_super_admin_user),
) -> dict[str, list[AuthConfigResponse]]:
    """Get all authentication configurations grouped by category.

    Returns all authentication configuration settings organized by their
    category (ldap, oidc, pki, mfa, password_policy, session, banner).

    Sensitive values are masked in the response.

    Returns:
        Dictionary with category names as keys and list of configs as values
    """
    logger.info(f"Auth configs requested by super admin {current_user.email}")

    result: dict[str, list[AuthConfigResponse]] = {}

    for category in VALID_CATEGORIES:
        configs = db.query(AuthConfig).filter(AuthConfig.category == category).all()
        result[category] = []

        for config in configs:
            # Never hand a secret — or a placeholder standing in for one — back to
            # the client. Returning the literal "***REDACTED***" here is what let
            # the admin panel bind it into the password field and submit it back,
            # overwriting the real credential on the next Save. `is_set` carries
            # the only thing the UI actually needs: whether a value exists.
            config_dict = {
                "id": config.id,
                "uuid": str(config.uuid),
                "config_key": config.config_key,
                "config_value": (None if config.is_sensitive else config.config_value),
                "is_set": bool(config.config_value),
                "is_sensitive": config.is_sensitive,
                "category": config.category,
                "data_type": config.data_type,
                "description": config.description,
                "requires_restart": config.requires_restart,
                "created_at": config.created_at,
                "updated_at": config.updated_at,
            }
            result[category].append(AuthConfigResponse(**config_dict))  # type: ignore[arg-type]

    return result


@router.get("/status", response_model=AuthConfigStatusResponse)
def get_auth_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_super_admin_user),
) -> AuthConfigStatusResponse:
    """Get the enabled/disabled status of each authentication method.

    Returns a summary of which authentication methods are currently enabled.

    Returns:
        Status object with boolean flags for each auth method
    """
    logger.info(f"Auth status requested by super admin {current_user.email}")
    status_dict = AuthConfigService.get_config_status(db)
    return AuthConfigStatusResponse(**status_dict)


@router.get("/{category}", response_model=dict[str, Any])
def get_config_by_category(
    category: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_super_admin_user),
) -> dict[str, Any]:
    """Get configuration for a specific category.

    Args:
        category: Configuration category (ldap, oidc, pki, etc.)

    Returns:
        Dictionary of configuration key-value pairs for the category

    Raises:
        HTTPException: If category is not valid
    """
    _require_valid_category(category)

    logger.info(f"Auth config category '{category}' requested by super admin {current_user.email}")

    # Get configs with sensitive values masked (not decrypted for display)
    return AuthConfigService.get_config_by_category(db, category, decrypt=False)


@router.put("/{category}", response_model=dict[str, Any])
def update_config_category(
    category: str,
    config: dict[str, Any],
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_super_admin_user),
) -> dict[str, Any]:
    """Update configuration for a category.

    Updates multiple configuration values for the specified category.
    All changes are logged to the audit table.

    The body stays an open dict because each category has its own key set; it is
    validated against that category's schema in ``bulk_update_category`` (unknown
    keys, unparseable values, out-of-range numbers and rejected combinations all
    become a 400). Nothing is written unless the whole payload validates.

    Args:
        category: Configuration category to update
        config: Dictionary of key-value pairs to update
        request: FastAPI request object for audit logging

    Returns:
        Success message and update count

    Raises:
        HTTPException: 400 if the category or payload is invalid, 500 if the
            update itself fails
    """
    _require_valid_category(category)

    # Snapshot the actor BEFORE any DB work: the refusal paths below audit after
    # db.rollback(), which expires every ORM instance in this session.
    actor_id, actor_email = int(current_user.id), str(current_user.email)

    logger.info(
        f"Auth config category '{category}' update by super admin {actor_email}: "
        f"{list(config.keys())}"
    )

    try:
        results = AuthConfigService.bulk_update_category(
            db=db,
            category=category,
            config_dict=config,
            user_id=current_user.id,
            request=request,
        )

        _audit_auth_config_event(
            request,
            actor_id,
            actor_email,
            action="auth_config_update",
            outcome=AuditOutcome.SUCCESS,
            details={"category": category, **_change_summary(results)},
        )

        return {
            "success": True,
            "message": f"{category} configuration updated",
            "updated_count": len(results),
            "updated_keys": list(results.keys()),
        }

    except ValueError as e:
        # A rejected payload is the caller's fault and its detail is safe to
        # return: it names the offending keys so the admin can fix the request.
        # Reaching the generic handler below would have turned it into a 500.
        db.rollback()
        # A refused change is auditable too: repeated attempts to disable a
        # control are exactly the pattern a reviewer is looking for, and a log
        # that records only successes cannot show them. Key NAMES only — the
        # rejected payload's values are unvalidated caller input and may include
        # a secret the admin meant to set.
        _audit_auth_config_event(
            request,
            actor_id,
            actor_email,
            action="auth_config_update",
            outcome=AuditOutcome.FAILURE,
            error_code="INVALID_AUTH_CONFIG",
            details={"category": category, "requested_keys": sorted(config)},
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.error("Failed to update %s config: %s", category, e, exc_info=True)
        db.rollback()
        _audit_auth_config_event(
            request,
            actor_id,
            actor_email,
            action="auth_config_update",
            outcome=AuditOutcome.FAILURE,
            error_code="AUTH_CONFIG_UPDATE_FAILED",
            details={"category": category, "requested_keys": sorted(config)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal error occurred. Please try again.",
        ) from e


@router.post("/{category}/test", response_model=AuthMethodTestResponse)
async def test_auth_connection(
    category: str,
    config: dict[str, Any],
    current_user: User = Depends(get_current_super_admin_user),
) -> AuthMethodTestResponse:
    """Test connection for LDAP or OIDC.

    Tests the provided configuration without saving it. Useful for
    validating settings before applying them.

    Args:
        category: Configuration category (ldap or oidc)
        config: Configuration values to test

    Returns:
        Test result with success status and message

    Raises:
        HTTPException: If test is not supported for the category
    """
    logger.info(f"Auth connection test for '{category}' by super admin {current_user.email}")

    if category == "ldap":
        # ldap3 is blocking; keep it off the event loop (issue #320).
        ldap_result: AuthMethodTestResponse = await run_in_threadpool(_test_ldap_connection, config)
        return ldap_result
    elif category == "oidc":
        return await _test_oidc_connection(config)
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Connection test not supported for category: {category}",
        )


@router.get("/audit/{category}", response_model=list[AuthConfigAuditResponse])
def get_audit_log(
    category: str,
    limit: int = Query(100, ge=1, le=MAX_AUDIT_LOG_LIMIT),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_super_admin_user),
) -> list[AuthConfigAuditResponse]:
    """Get audit log for configuration changes.

    Returns audit log entries for the specified category, ordered by
    most recent first.

    Args:
        category: Configuration category to filter by
        limit: Maximum number of entries to return (default 100)
        offset: Number of entries to skip for pagination

    Returns:
        List of audit log entries

    Raises:
        HTTPException: 400 if the category is not valid. This route had no
            category check at all, and ``get_audit_log`` skipped its filter for an
            unrecognised category — so ``/audit/anything`` returned the ENTIRE
            audit log, every category, unfiltered.
    """
    _require_valid_category(category)

    logger.info(
        f"Auth config audit log for '{category}' requested by super admin {current_user.email}"
    )

    audits = AuthConfigService.get_audit_log(
        db=db,
        category=category,
        limit=limit,
        offset=offset,
    )

    # Resolve the actors in one query rather than per row. Since v387 `changed_by` is
    # nullable and ON DELETE SET NULL, so it is genuinely NULL once the author's
    # account is deleted — this None branch used to be unreachable. Either way a miss
    # renders as unknown rather than dropping the entry: losing the record of a change
    # because its author left is the opposite of an audit trail.
    actor_ids = {audit.changed_by for audit in audits if audit.changed_by is not None}
    actor_emails: dict[int, str] = {}
    if actor_ids:
        actor_emails = {
            row.id: row.email
            for row in db.query(User.id, User.email).filter(User.id.in_(actor_ids)).all()
        }

    return [
        AuthConfigAuditResponse(
            id=audit.id,  # type: ignore[arg-type]
            uuid=str(audit.uuid),
            config_key=audit.config_key,  # type: ignore[arg-type]
            old_value=audit.old_value,  # type: ignore[arg-type]
            new_value=audit.new_value,  # type: ignore[arg-type]
            change_type=audit.change_type,  # type: ignore[arg-type]
            changed_by_email=actor_emails.get(audit.changed_by),  # type: ignore[arg-type]
            ip_address=audit.ip_address,  # type: ignore[arg-type]
            created_at=audit.created_at,  # type: ignore[arg-type]
        )
        for audit in audits
    ]


@router.post("/migrate")
def migrate_from_env(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_super_admin_user),
) -> dict[str, Any]:
    """One-time migration from .env to database.

    Migrates authentication configuration from environment variables to
    the database. Only migrates values that don't already exist in the
    database. This is typically run once during initial setup or upgrade.

    Returns:
        Success message with count of migrated settings
    """
    # Snapshot the actor before any DB work, for the same reason the category
    # update does: the failure path rolls back and expires every ORM instance.
    actor_id, actor_email = int(current_user.id), str(current_user.email)

    logger.info(f"Auth config migration from env initiated by super admin {actor_email}")

    try:
        count = AuthConfigService.migrate_from_env(db, actor_id)

        # Bulk-seeds every unset auth key from the environment, so it is a
        # configuration change of the widest possible blast radius — including
        # the enable flags for every auth method — under one request.
        _audit_auth_config_event(
            request,
            actor_id,
            actor_email,
            action="auth_config_migrate_from_env",
            outcome=AuditOutcome.SUCCESS,
            details={"migrated_count": count},
        )

        return {
            "success": True,
            "migrated_count": count,
            "message": f"Successfully migrated {count} settings from environment to database",
        }

    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.error("Migration failed: %s", e, exc_info=True)
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal error occurred. Please try again.",
        ) from e


def _test_ldap_connection(config: dict[str, Any]) -> AuthMethodTestResponse:
    """Test LDAP connection with provided configuration.

    Synchronous: ``ldap3`` binds are blocking with a 10 s connect timeout, so the
    caller must run this off the event loop (issue #320).

    Args:
        config: LDAP configuration to test

    Returns:
        Test result with success status, message, and optional details
    """
    try:
        import ldap3
        from ldap3.core.exceptions import LDAPException

        server_address = config.get("ldap_server", "")
        port = config.get("ldap_port", 636)
        use_ssl = config.get("ldap_use_ssl", True)

        if not server_address:
            return AuthMethodTestResponse(
                success=False,
                message="LDAP server address is required",
            )

        logger.info(f"Testing LDAP connection to {server_address}:{port} (SSL={use_ssl})")

        server = ldap3.Server(
            server_address,
            port=port,
            use_ssl=use_ssl,
            get_info=ldap3.ALL,
            connect_timeout=10,
        )

        bind_dn = config.get("ldap_bind_dn", "")
        bind_password = config.get("ldap_bind_password", "")

        if bind_dn and bind_password:
            # Try authenticated bind
            conn = ldap3.Connection(
                server,
                user=bind_dn,
                password=bind_password,
                auto_bind=True,
                receive_timeout=10,
            )
        else:
            # Try anonymous bind
            conn = ldap3.Connection(
                server,
                auto_bind=True,
                receive_timeout=10,
            )

        # Get server info
        server_info = {}
        if server.info:
            server_info = {
                "vendor_name": str(server.info.vendor_name) if server.info.vendor_name else None,
                "vendor_version": (
                    str(server.info.vendor_version) if server.info.vendor_version else None
                ),
                "naming_contexts": (
                    [str(nc) for nc in server.info.naming_contexts]
                    if server.info.naming_contexts
                    else []
                ),
            }

        conn.unbind()

        logger.info(f"LDAP connection test successful to {server_address}")

        return AuthMethodTestResponse(
            success=True,
            message="LDAP connection successful",
            details={"server_info": server_info},
        )

    except LDAPException as e:
        logger.warning(f"LDAP connection test failed: {e}")
        return AuthMethodTestResponse(
            success=False,
            message="LDAP connection failed. Please verify server address, port, and credentials.",
        )
    except ImportError:
        return AuthMethodTestResponse(
            success=False,
            message="LDAP library (ldap3) is not installed",
        )
    except Exception as e:
        logger.exception(f"LDAP connection test error: {e}")
        return AuthMethodTestResponse(
            success=False,
            message="LDAP connection failed due to an unexpected error. Check server logs for details.",
        )


def _roles_claim_advertised(configured_roles_claim: str, claims_supported: Any) -> str:
    """Cross-reference the configured roles claim against the discovery document.

    Best-effort only, and says so via ``"unknown"``: ``claims_supported`` is an
    OPTIONAL discovery field (OIDC Discovery §3), several real providers omit it
    entirely, and only the top-level segment of a dotted path (``realm_access`` of
    ``realm_access.roles``) is a claim NAME — the rest is a JSON path into it,
    which a claims list was never going to enumerate.

    Args:
        configured_roles_claim: The admin's configured dotted claim path.
        claims_supported: The discovery document's ``claims_supported`` value, if
            the provider sent one.

    Returns:
        ``"yes"``, ``"no"``, or ``"unknown"``.
    """
    if not isinstance(claims_supported, list) or not claims_supported:
        return "unknown"
    if not configured_roles_claim:
        return "unknown"
    top_level = configured_roles_claim.split(".", 1)[0]
    return "yes" if top_level in claims_supported else "no"


async def _test_oidc_connection(config: dict[str, Any]) -> AuthMethodTestResponse:
    """Test the OIDC provider connection with the supplied configuration.

    Resolves the metadata URL exactly the way the login path does — the explicit
    discovery URL when one is configured, the realm form otherwise. It previously
    always built the realm form, so on a provider that does not serve that URL shape
    this button reported a failure for a configuration that was in fact correct, and
    on a realm provider it tested a URL the login path might not use.

    Args:
        config: OIDC configuration to test

    Returns:
        Test result with success status, message, and optional details
    """
    try:
        import httpx

        from app.utils.url_validation import assert_safe_outbound_url

        server_url = config.get("oidc_server_url", "")
        realm = config.get("oidc_realm", "opentranscribe")
        discovery_url = (config.get("oidc_discovery_url") or "").strip()

        if not discovery_url and not server_url:
            return AuthMethodTestResponse(
                success=False,
                message="Provide either a discovery URL or a server URL",
            )

        if discovery_url:
            well_known_url = discovery_url
        else:
            # Remove trailing slash if present
            server_url = server_url.rstrip("/")
            well_known_url = f"{server_url}/realms/{realm}/.well-known/openid-configuration"

        # This endpoint fetches a super_admin-supplied URL, which is a classic
        # SSRF primitive — it had no guard at all. Private targets stay allowed
        # because an IdP on the LAN or the compose network is a legitimate
        # deployment; what this blocks is cloud instance metadata and friends.
        try:
            assert_safe_outbound_url(
                well_known_url,
                purpose="OIDC discovery test-connection",
                allow_private=True,
            )
        except HTTPException:
            return AuthMethodTestResponse(
                success=False,
                message="That URL is not an allowed outbound target.",
            )

        logger.info(f"Testing OIDC connection to {well_known_url}")

        async with httpx.AsyncClient(timeout=10.0, verify=True) as client:
            response = await client.get(well_known_url)

            if response.status_code == 200:
                oidc_config = response.json()

                # Extract relevant endpoints for display
                details: dict[str, Any] = {
                    "issuer": oidc_config.get("issuer"),
                    "authorization_endpoint": oidc_config.get("authorization_endpoint"),
                    "token_endpoint": oidc_config.get("token_endpoint"),
                    "userinfo_endpoint": oidc_config.get("userinfo_endpoint"),
                    "supported_grant_types": oidc_config.get("grant_types_supported", [])[
                        :5
                    ],  # Limit for readability
                    "supported_scopes": oidc_config.get("scopes_supported", [])[:10],
                }

                # P1.2: this is the only signal available before a real login has
                # happened. `claims_supported` is OPTIONAL per Discovery §3 — a
                # provider that omits it (Authentik does) leaves the check
                # "unknown", not "not advertised", since silence isn't a claim.
                claims_supported = oidc_config.get("claims_supported")
                details["claims_supported"] = (
                    claims_supported[:30] if isinstance(claims_supported, list) else None
                )
                configured_roles_claim = (config.get("oidc_roles_claim") or "").strip()
                details["configured_roles_claim"] = configured_roles_claim or None
                details["roles_claim_advertised"] = _roles_claim_advertised(
                    configured_roles_claim, claims_supported
                )

                logger.info(f"OIDC connection test successful to {server_url}")

                return AuthMethodTestResponse(
                    success=True,
                    message="OIDC connection successful",
                    details=details,
                )
            else:
                logger.warning(
                    f"OIDC connection test failed with status {response.status_code}: "
                    f"{response.text[:200]}"
                )
                return AuthMethodTestResponse(
                    success=False,
                    message=f"The identity provider returned HTTP status {response.status_code}. Check server logs for details.",
                )

    except httpx.ConnectError as e:
        logger.warning(f"OIDC connection test failed: {e}")
        return AuthMethodTestResponse(
            success=False,
            message="Could not connect to the identity provider. Please verify the server URL and network connectivity.",
        )
    except httpx.TimeoutException:
        return AuthMethodTestResponse(
            success=False,
            message="Connection to the identity provider timed out",
        )
    except Exception as e:
        logger.exception(f"OIDC connection test error: {e}")
        return AuthMethodTestResponse(
            success=False,
            message="OIDC connection failed due to an unexpected error. Check server logs for details.",
        )
