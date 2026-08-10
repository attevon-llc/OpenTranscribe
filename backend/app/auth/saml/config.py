"""Resolved SAML 2.0 configuration (database first, environment second).

Mirrors ``app.auth.oidc.config.OIDCConfig`` — same DB > .env > default layering,
same frozen-per-request shape so an admin saving the auth tab mid-flow cannot change
what an in-flight login validates against.
"""

import logging
from dataclasses import dataclass

from app.core.config import settings as env_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SAMLConfig:
    """Immutable SAML configuration resolved from database or environment."""

    enabled: bool = False
    sp_entity_id: str = ""
    sp_acs_url: str = ""
    sp_sls_url: str = ""
    sp_x509_cert: str = ""
    sp_private_key: str = ""
    idp_entity_id: str = ""
    idp_sso_url: str = ""
    idp_slo_url: str = ""
    idp_x509_cert: str = ""
    want_assertions_signed: bool = True
    want_messages_signed: bool = True
    sign_authn_requests: bool = False
    email_attribute: str = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress"
    name_attribute: str = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name"
    groups_attribute: str = "groups"
    admin_group: str = ""
    #: Semicolon-delimited, mirroring ``OIDCConfig.allowed_groups``/``blocked_groups``
    #: exactly — empty admits everyone, blocked is evaluated first.
    allowed_groups: str = ""
    blocked_groups: str = ""

    @classmethod
    def from_env(cls) -> "SAMLConfig":
        """Create config from environment variables only."""
        return cls(
            enabled=env_settings.SAML_ENABLED,
            sp_entity_id=env_settings.SAML_SP_ENTITY_ID,
            sp_acs_url=env_settings.SAML_SP_ACS_URL,
            sp_sls_url=env_settings.SAML_SP_SLS_URL,
            sp_x509_cert=env_settings.SAML_SP_X509_CERT,
            sp_private_key=env_settings.SAML_SP_PRIVATE_KEY,
            idp_entity_id=env_settings.SAML_IDP_ENTITY_ID,
            idp_sso_url=env_settings.SAML_IDP_SSO_URL,
            idp_slo_url=env_settings.SAML_IDP_SLO_URL,
            idp_x509_cert=env_settings.SAML_IDP_X509_CERT,
            want_assertions_signed=env_settings.SAML_WANT_ASSERTIONS_SIGNED,
            want_messages_signed=env_settings.SAML_WANT_MESSAGES_SIGNED,
            sign_authn_requests=env_settings.SAML_SIGN_AUTHN_REQUESTS,
            email_attribute=env_settings.SAML_EMAIL_ATTRIBUTE,
            name_attribute=env_settings.SAML_NAME_ATTRIBUTE,
            groups_attribute=env_settings.SAML_GROUPS_ATTRIBUTE,
            admin_group=env_settings.SAML_ADMIN_GROUP,
            allowed_groups=env_settings.SAML_ALLOWED_GROUPS,
            blocked_groups=env_settings.SAML_BLOCKED_GROUPS,
        )

    @classmethod
    def from_db(cls, db) -> "SAMLConfig":
        """Create config from database with env fallback.

        Uses ``DynamicAuthSettings``, which checks DB > .env > coded default.
        """
        from app.core.auth_settings import get_auth_settings

        auth = get_auth_settings(db)

        def _get(key: str, default):
            val = auth.get(key)
            return val if val is not None else default

        def _get_bool(key: str, default: bool) -> bool:
            val = auth.get(key)
            if val is None:
                return default
            if isinstance(val, bool):
                return val
            if isinstance(val, str):
                return val.lower() in ("true", "1", "yes", "on")
            return bool(val)

        return cls(
            enabled=_get_bool("saml_enabled", env_settings.SAML_ENABLED),
            sp_entity_id=str(_get("saml_sp_entity_id", env_settings.SAML_SP_ENTITY_ID) or ""),
            sp_acs_url=str(_get("saml_sp_acs_url", env_settings.SAML_SP_ACS_URL) or ""),
            sp_sls_url=str(_get("saml_sp_sls_url", env_settings.SAML_SP_SLS_URL) or ""),
            sp_x509_cert=str(_get("saml_sp_x509_cert", env_settings.SAML_SP_X509_CERT) or ""),
            sp_private_key=str(_get("saml_sp_private_key", env_settings.SAML_SP_PRIVATE_KEY) or ""),
            idp_entity_id=str(_get("saml_idp_entity_id", env_settings.SAML_IDP_ENTITY_ID) or ""),
            idp_sso_url=str(_get("saml_idp_sso_url", env_settings.SAML_IDP_SSO_URL) or ""),
            idp_slo_url=str(_get("saml_idp_slo_url", env_settings.SAML_IDP_SLO_URL) or ""),
            idp_x509_cert=str(_get("saml_idp_x509_cert", env_settings.SAML_IDP_X509_CERT) or ""),
            want_assertions_signed=_get_bool(
                "saml_want_assertions_signed", env_settings.SAML_WANT_ASSERTIONS_SIGNED
            ),
            want_messages_signed=_get_bool(
                "saml_want_messages_signed", env_settings.SAML_WANT_MESSAGES_SIGNED
            ),
            sign_authn_requests=_get_bool(
                "saml_sign_authn_requests", env_settings.SAML_SIGN_AUTHN_REQUESTS
            ),
            email_attribute=str(
                _get("saml_email_attribute", env_settings.SAML_EMAIL_ATTRIBUTE) or ""
            ),
            name_attribute=str(_get("saml_name_attribute", env_settings.SAML_NAME_ATTRIBUTE) or ""),
            groups_attribute=str(
                _get("saml_groups_attribute", env_settings.SAML_GROUPS_ATTRIBUTE) or "groups"
            ),
            admin_group=str(_get("saml_admin_group", env_settings.SAML_ADMIN_GROUP) or ""),
            allowed_groups=str(_get("saml_allowed_groups", env_settings.SAML_ALLOWED_GROUPS) or ""),
            blocked_groups=str(_get("saml_blocked_groups", env_settings.SAML_BLOCKED_GROUPS) or ""),
        )
