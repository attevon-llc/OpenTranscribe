"""API endpoints for authentication configuration management.

This module provides REST API endpoints for super admin users to manage
authentication configuration settings including LDAP, Keycloak, PKI,
MFA, password policy, and session configurations.

All endpoints require super_admin role and include audit logging for
compliance requirements (FedRAMP, NIST 800-53).
"""

import logging
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_active_user
from app.db.base import get_db
from app.models.auth_config import AuthConfig
from app.models.user import User
from app.schemas.auth_config import AuthConfigAuditResponse
from app.schemas.auth_config import AuthConfigResponse
from app.schemas.auth_config import AuthConfigStatusResponse
from app.schemas.auth_config import AuthMethodTestResponse
from app.services.auth_config_service import AuthConfigService

router = APIRouter()
logger = logging.getLogger(__name__)


def get_current_super_admin_user(
    current_user: User = Depends(get_current_active_user),
) -> User:
    """Verify user has super_admin role.

    Args:
        current_user: Currently authenticated user

    Returns:
        User if they have super_admin role

    Raises:
        HTTPException: If user does not have super_admin role
    """
    if current_user.role != "super_admin":
        logger.warning(
            f"User {current_user.email} (role={current_user.role}) "
            "attempted to access super admin endpoint"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Super admin access required",
        )
    return current_user


@router.get("", response_model=dict[str, list[AuthConfigResponse]])
async def get_all_configs(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_super_admin_user),
) -> dict[str, list[AuthConfigResponse]]:
    """Get all authentication configurations grouped by category.

    Returns all authentication configuration settings organized by their
    category (ldap, keycloak, pki, mfa, password_policy, session, banner).

    Sensitive values are masked in the response.

    Returns:
        Dictionary with category names as keys and list of configs as values
    """
    logger.info(f"Auth configs requested by super admin {current_user.email}")

    categories = [
        "local",
        "ldap",
        "keycloak",
        "pki",
        "password_policy",
        "mfa",
        "session",
        "banner",
        "lockout",
    ]
    result: dict[str, list[AuthConfigResponse]] = {}

    for category in categories:
        configs = db.query(AuthConfig).filter(AuthConfig.category == category).all()
        result[category] = []

        for config in configs:
            # Mask sensitive values in response
            config_dict = {
                "id": config.id,
                "uuid": str(config.uuid),
                "config_key": config.config_key,
                "config_value": ("***REDACTED***" if config.is_sensitive else config.config_value),
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
async def get_auth_status(
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
async def get_config_by_category(
    category: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_super_admin_user),
) -> dict[str, Any]:
    """Get configuration for a specific category.

    Args:
        category: Configuration category (ldap, keycloak, pki, etc.)

    Returns:
        Dictionary of configuration key-value pairs for the category

    Raises:
        HTTPException: If category is not valid
    """
    valid_categories = [
        "local",
        "ldap",
        "keycloak",
        "pki",
        "password_policy",
        "mfa",
        "session",
        "banner",
        "lockout",
    ]

    if category not in valid_categories:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid category. Must be one of: {', '.join(valid_categories)}",
        )

    logger.info(f"Auth config category '{category}' requested by super admin {current_user.email}")

    # Get configs with sensitive values masked (not decrypted for display)
    return AuthConfigService.get_config_by_category(db, category, decrypt=False)


@router.put("/{category}", response_model=dict[str, Any])
async def update_config_category(
    category: str,
    config: dict[str, Any],
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_super_admin_user),
) -> dict[str, Any]:
    """Update configuration for a category.

    Updates multiple configuration values for the specified category.
    All changes are logged to the audit table.

    Args:
        category: Configuration category to update
        config: Dictionary of key-value pairs to update
        request: FastAPI request object for audit logging

    Returns:
        Success message and update count

    Raises:
        HTTPException: If category is not valid or update fails
    """
    valid_categories = [
        "local",
        "ldap",
        "keycloak",
        "pki",
        "password_policy",
        "mfa",
        "session",
        "banner",
        "lockout",
    ]

    if category not in valid_categories:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid category. Must be one of: {', '.join(valid_categories)}",
        )

    logger.info(
        f"Auth config category '{category}' update by super admin {current_user.email}: "
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

        return {
            "success": True,
            "message": f"{category} configuration updated",
            "updated_count": len(results),
            "updated_keys": list(results.keys()),
        }

    except Exception as e:
        logger.error("Failed to update %s config: %s", category, e, exc_info=True)
        db.rollback()
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
    """Test connection for LDAP or Keycloak.

    Tests the provided configuration without saving it. Useful for
    validating settings before applying them.

    Args:
        category: Configuration category (ldap or keycloak)
        config: Configuration values to test

    Returns:
        Test result with success status and message

    Raises:
        HTTPException: If test is not supported for the category
    """
    logger.info(f"Auth connection test for '{category}' by super admin {current_user.email}")

    if category == "ldap":
        return await _test_ldap_connection(config)
    elif category == "keycloak":
        return await _test_keycloak_connection(config)
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Connection test not supported for category: {category}",
        )


@router.get("/audit/{category}", response_model=list[AuthConfigAuditResponse])
async def get_audit_log(
    category: str,
    limit: int = 100,
    offset: int = 0,
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
    """
    logger.info(
        f"Auth config audit log for '{category}' requested by super admin {current_user.email}"
    )

    audits = AuthConfigService.get_audit_log(
        db=db,
        category=category,
        limit=limit,
        offset=offset,
    )

    return [
        AuthConfigAuditResponse(
            id=audit.id,  # type: ignore[arg-type]
            uuid=str(audit.uuid),
            config_key=audit.config_key,  # type: ignore[arg-type]
            old_value=audit.old_value,  # type: ignore[arg-type]
            new_value=audit.new_value,  # type: ignore[arg-type]
            change_type=audit.change_type,  # type: ignore[arg-type]
            ip_address=audit.ip_address,  # type: ignore[arg-type]
            created_at=audit.created_at,  # type: ignore[arg-type]
        )
        for audit in audits
    ]


@router.post("/migrate")
async def migrate_from_env(
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
    logger.info(f"Auth config migration from env initiated by super admin {current_user.email}")

    try:
        count = AuthConfigService.migrate_from_env(db, current_user.id)

        return {
            "success": True,
            "migrated_count": count,
            "message": f"Successfully migrated {count} settings from environment to database",
        }

    except Exception as e:
        logger.error("Migration failed: %s", e, exc_info=True)
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal error occurred. Please try again.",
        ) from e


async def _test_ldap_connection(config: dict[str, Any]) -> AuthMethodTestResponse:
    """Test LDAP connection with provided configuration.

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
        logger.error(f"LDAP connection test error: {e}")
        return AuthMethodTestResponse(
            success=False,
            message="LDAP connection failed due to an unexpected error. Check server logs for details.",
        )


async def _test_keycloak_connection(config: dict[str, Any]) -> AuthMethodTestResponse:
    """Test Keycloak connection with provided configuration.

    Args:
        config: Keycloak configuration to test

    Returns:
        Test result with success status, message, and optional details
    """
    try:
        import httpx

        server_url = config.get("keycloak_server_url", "")
        realm = config.get("keycloak_realm", "opentranscribe")

        if not server_url:
            return AuthMethodTestResponse(
                success=False,
                message="Keycloak server URL is required",
            )

        # Build the well-known endpoint URL
        # Remove trailing slash if present
        server_url = server_url.rstrip("/")
        well_known_url = f"{server_url}/realms/{realm}/.well-known/openid-configuration"

        logger.info(f"Testing Keycloak connection to {well_known_url}")

        async with httpx.AsyncClient(timeout=10.0, verify=True) as client:
            response = await client.get(well_known_url)

            if response.status_code == 200:
                oidc_config = response.json()

                # Extract relevant endpoints for display
                details = {
                    "issuer": oidc_config.get("issuer"),
                    "authorization_endpoint": oidc_config.get("authorization_endpoint"),
                    "token_endpoint": oidc_config.get("token_endpoint"),
                    "userinfo_endpoint": oidc_config.get("userinfo_endpoint"),
                    "supported_grant_types": oidc_config.get("grant_types_supported", [])[
                        :5
                    ],  # Limit for readability
                    "supported_scopes": oidc_config.get("scopes_supported", [])[:10],
                }

                logger.info(f"Keycloak connection test successful to {server_url}")

                return AuthMethodTestResponse(
                    success=True,
                    message="Keycloak connection successful",
                    details=details,
                )
            else:
                logger.warning(
                    f"Keycloak connection test failed with status {response.status_code}: "
                    f"{response.text[:200]}"
                )
                return AuthMethodTestResponse(
                    success=False,
                    message=f"Keycloak returned HTTP status {response.status_code}. Check server logs for details.",
                )

    except httpx.ConnectError as e:
        logger.warning(f"Keycloak connection test failed: {e}")
        return AuthMethodTestResponse(
            success=False,
            message="Could not connect to Keycloak server. Please verify the server URL and network connectivity.",
        )
    except httpx.TimeoutException:
        return AuthMethodTestResponse(
            success=False,
            message="Connection to Keycloak server timed out",
        )
    except Exception as e:
        logger.error(f"Keycloak connection test error: {e}")
        return AuthMethodTestResponse(
            success=False,
            message="Keycloak connection failed due to an unexpected error. Check server logs for details.",
        )
