"""Startup checks for the authentication settings (LDAP, OIDC, PKI).

``Settings`` calls these after loading; each raises ``ValueError`` for a configuration
that cannot work, and the LDAP check warns when SSL and StartTLS are both set.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.config import Settings

_config_logger = logging.getLogger(__name__)


def validate_ldap_settings(settings: Settings) -> None:
    """Validate LDAP configuration when LDAP authentication is enabled.

    Args:
        settings: The Settings instance to validate.

    Raises:
        ValueError: If LDAP_ENABLED is true but required LDAP fields are missing.
    """
    if not settings.LDAP_ENABLED:
        return

    missing_ldap = []
    if not settings.LDAP_SERVER:
        missing_ldap.append("LDAP_SERVER")
    if not settings.LDAP_BIND_DN:
        missing_ldap.append("LDAP_BIND_DN")
    if not settings.LDAP_BIND_PASSWORD:
        missing_ldap.append("LDAP_BIND_PASSWORD")
    if not settings.LDAP_SEARCH_BASE:
        missing_ldap.append("LDAP_SEARCH_BASE")
    if missing_ldap:
        raise ValueError(
            f"LDAP_ENABLED=true but the following required settings are missing: "
            f"{', '.join(missing_ldap)}"
        )

    if settings.LDAP_USE_SSL and settings.LDAP_USE_TLS:
        # ldap3 lets the URL scheme override use_ssl, so the effective transport follows the
        # server address: an explicit ldap:// URL is never LDAPS, and StartTLS then applies.
        if settings.LDAP_SERVER.lower().startswith("ldap://"):
            _config_logger.warning(
                "LDAP_USE_SSL and LDAP_USE_TLS are mutually exclusive, and LDAP_SERVER is an "
                "ldap:// URL, which is never LDAPS. Using StartTLS (LDAP_USE_TLS); "
                "LDAP_USE_SSL is ignored."
            )
        else:
            _config_logger.warning(
                "LDAP_USE_SSL and LDAP_USE_TLS are mutually exclusive; using LDAPS, not StartTLS."
            )


def validate_oidc_settings(settings: Settings) -> None:
    """Validate OIDC configuration when OIDC authentication is enabled.

    Args:
        settings: The Settings instance to validate.

    Raises:
        ValueError: If OIDC_ENABLED is true but required OIDC fields are missing.
    """
    if not settings.OIDC_ENABLED:
        return

    missing_oidc = []
    if not settings.OIDC_SERVER_URL:
        missing_oidc.append("OIDC_SERVER_URL")
    if not settings.OIDC_CLIENT_ID:
        missing_oidc.append("OIDC_CLIENT_ID")
    if not settings.OIDC_CALLBACK_URL:
        missing_oidc.append("OIDC_CALLBACK_URL")
    if missing_oidc:
        raise ValueError(
            f"OIDC_ENABLED=true but the following required settings are missing: "
            f"{', '.join(missing_oidc)}"
        )


def validate_pki_settings(settings: Settings) -> None:
    """Validate PKI configuration when PKI authentication with revocation checking is enabled.

    Args:
        settings: The Settings instance to validate.

    Raises:
        ValueError: If PKI_ENABLED and PKI_VERIFY_REVOCATION are true but PKI_CA_CERT_PATH is missing.
    """
    if settings.PKI_ENABLED and settings.PKI_VERIFY_REVOCATION and not settings.PKI_CA_CERT_PATH:
        raise ValueError(
            "PKI_VERIFY_REVOCATION=true but PKI_CA_CERT_PATH is not set. "
            "CA certificate is required for OCSP revocation checking."
        )
