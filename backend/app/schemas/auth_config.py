"""Pydantic schemas for authentication configuration."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel
from pydantic import Field


class AuthConfigBase(BaseModel):
    """Base schema for authentication configuration."""

    config_key: str = Field(..., max_length=100)
    config_value: str | None = None
    is_sensitive: bool = False
    category: str = Field(..., max_length=50)
    data_type: str = Field(default="string", max_length=20)
    description: str | None = None
    requires_restart: bool = False


class AuthConfigCreate(AuthConfigBase):
    """Schema for creating a new authentication configuration."""


class AuthConfigUpdate(BaseModel):
    """Schema for updating an authentication configuration."""

    config_value: str | None = None
    description: str | None = None


class AuthConfigResponse(AuthConfigBase):
    """Schema for authentication configuration response."""

    id: int
    uuid: str
    #: Whether a value is stored. For a sensitive key ``config_value`` is always
    #: ``None`` on the wire, so this is how the admin UI renders "a secret is
    #: configured — leave blank to keep it" without ever receiving the secret.
    is_set: bool = False
    created_at: datetime
    updated_at: datetime

    class Config:
        """Pydantic configuration."""

        from_attributes = True


class AuthConfigAuditResponse(BaseModel):
    """Schema for authentication configuration audit log response."""

    id: int
    uuid: str
    config_key: str
    old_value: str | None = None  # Will be masked for sensitive
    new_value: str | None = None  # Will be masked for sensitive
    change_type: str
    ip_address: str | None = None
    created_at: datetime

    class Config:
        """Pydantic configuration."""

        from_attributes = True


# Category-specific configuration schemas


class LDAPConfig(BaseModel):
    """LDAP/Active Directory configuration."""

    ldap_enabled: bool = False
    ldap_server: str = ""
    ldap_port: int = 636
    ldap_use_ssl: bool = True
    ldap_use_tls: bool = False
    ldap_bind_dn: str = ""
    ldap_bind_password: str | None = None  # Sensitive
    ldap_search_base: str = ""
    ldap_username_attr: str = "sAMAccountName"
    ldap_email_attr: str = "mail"
    ldap_name_attr: str = "cn"
    ldap_timeout: int = 10
    ldap_admin_users: str = ""
    ldap_admin_groups: str = ""
    ldap_user_groups: str = ""
    ldap_recursive_groups: bool = False
    ldap_group_attr: str = "memberOf"


class KeycloakConfig(BaseModel):
    """Keycloak/OIDC configuration."""

    keycloak_enabled: bool = False
    keycloak_server_url: str = ""
    keycloak_internal_url: str = ""
    keycloak_realm: str = "opentranscribe"
    keycloak_client_id: str = ""
    keycloak_client_secret: str | None = None  # Sensitive
    keycloak_callback_url: str = ""
    keycloak_admin_role: str = "admin"
    keycloak_timeout: int = 30
    keycloak_verify_audience: bool = True
    keycloak_audience: str = ""
    keycloak_use_pkce: bool = True
    keycloak_verify_issuer: bool = True


class PKIConfig(BaseModel):
    """PKI/X.509 certificate configuration."""

    pki_enabled: bool = False
    pki_ca_cert_path: str = ""
    pki_verify_revocation: bool = False
    pki_cert_header: str = "X-Client-Cert"
    pki_cert_dn_header: str = "X-Client-Cert-DN"
    pki_admin_dns: str = ""
    pki_ocsp_timeout_seconds: int = 5
    pki_crl_cache_seconds: int = 3600
    pki_revocation_soft_fail: bool = True
    pki_trusted_proxies: str = ""
    pki_mode: str = "direct"  # direct, keycloak, hybrid
    pki_allow_password_fallback: bool = True
    pki_support_cac: bool = True
    pki_support_piv: bool = True


class PasswordPolicyConfig(BaseModel):
    """Password policy configuration."""

    password_policy_enabled: bool = True
    password_min_length: int = 12
    password_require_uppercase: bool = True
    password_require_lowercase: bool = True
    password_require_digit: bool = True
    password_require_special: bool = True
    password_history_count: int = 24
    password_max_age_days: int = 60


class MFAConfig(BaseModel):
    """Multi-factor authentication configuration."""

    mfa_enabled: bool = False
    mfa_required: bool = False
    mfa_issuer_name: str = "OpenTranscribe"
    mfa_backup_code_count: int = 10
    mfa_token_expire_minutes: int = 5


class SessionConfig(BaseModel):
    """Session and token configuration."""

    jwt_access_token_expire_minutes: int = 60
    jwt_refresh_token_expire_days: int = 7
    session_idle_timeout_minutes: int = 15
    session_absolute_timeout_minutes: int = 480
    max_concurrent_sessions: int = 5
    concurrent_session_policy: str = "terminate_oldest"


class LoginBannerConfig(BaseModel):
    """Login banner configuration (FedRAMP AC-8)."""

    login_banner_enabled: bool = False
    login_banner_text: str = ""
    login_banner_classification: str = "UNCLASSIFIED"


class AuthMethodTestRequest(BaseModel):
    """Request to test authentication method connection."""

    category: str  # ldap or keycloak
    config: dict[str, Any]


class AuthMethodTestResponse(BaseModel):
    """Response from authentication method test."""

    success: bool
    message: str
    details: dict[str, Any] | None = None


class BulkConfigUpdate(BaseModel):
    """Schema for updating multiple configuration values at once."""

    category: str
    config: dict[str, Any]


class AuthConfigCategoryResponse(BaseModel):
    """Response schema for a category of auth configurations."""

    category: str
    configs: dict[str, Any]


class AuthConfigStatusResponse(BaseModel):
    """Response schema for overall auth configuration status."""

    ldap_enabled: bool = False
    keycloak_enabled: bool = False
    pki_enabled: bool = False
    mfa_enabled: bool = False
    password_policy_enabled: bool = True
    login_banner_enabled: bool = False
