"""Pydantic schemas for authentication configuration.

The per-category models near the bottom of this file are not documentation: they
are the **allow-list, type contract, and bounds** that ``PUT /admin/auth-config/
{category}`` validates against (``validate_category_config``), the source of
``AuthConfigService.CONFIG_CATEGORIES``, and the source of the coded default a
malformed stored value falls back to. They used to be dead code while the write
path accepted a bare ``dict[str, Any]`` and stored every key verbatim.

Defaults here must agree with ``app/core/config.py`` — that module is the
authority, this one mirrors it.
"""

from datetime import datetime
from typing import Any
from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError


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
    #: Who made the change. ``auth_config_audit.changed_by`` is a NOT NULL FK and
    #: was never serialised, so the answer to "who turned MFA off / changed the
    #: LDAP bind password" sat in Postgres and was invisible in the product —
    #: which is most of the point of an audit trail. Nullable on the wire only
    #: because the referenced account may since have been deleted.
    changed_by_email: str | None = None
    ip_address: str | None = None
    created_at: datetime

    class Config:
        """Pydantic configuration."""

        from_attributes = True


# Category-specific configuration schemas
#
# Invariants enforced by tests/unit/test_auth_config_validation.py:
#   * every field has a default (the write path validates PARTIAL payloads, so a
#     required field would make an unrelated single-field save fail);
#   * no config key appears in two categories — ``auth_config.config_key`` is
#     globally UNIQUE, so a key claimed by two tabs lands in whichever category
#     wrote it first and then goes missing from the other tab's GET.


class _CategoryConfig(BaseModel):
    """Base for the per-category models.

    ``extra="forbid"`` is what turns a typo'd key (``keycloak_verify_audiance``)
    into a 400 instead of a row that is stored forever and read by nothing.
    """

    model_config = ConfigDict(extra="forbid")


class LocalAuthConfig(_CategoryConfig):
    """Local (password) identity-source configuration.

    The password-policy and MFA keys the admin UI's Local tab also renders belong
    to ``password_policy`` / ``mfa`` — see the uniqueness note above.
    """

    local_enabled: bool = True
    allow_registration: bool = True
    require_email_verification: bool = False
    mfa_issuer: str = "OpenTranscribe"
    password_require_numbers: bool = True
    max_login_attempts: int = Field(default=5, ge=1, le=1000)
    lockout_duration_minutes: int = Field(default=15, ge=1, le=10080)


class LDAPConfig(_CategoryConfig):
    """LDAP/Active Directory configuration."""

    ldap_enabled: bool = False
    ldap_server: str = ""
    ldap_port: int = Field(default=636, ge=1, le=65535)
    ldap_use_ssl: bool = True
    ldap_use_tls: bool = False
    ldap_bind_dn: str = ""
    ldap_bind_password: str | None = None  # Sensitive
    ldap_search_base: str = ""
    ldap_username_attr: str = "sAMAccountName"
    ldap_email_attr: str = "mail"
    ldap_name_attr: str = "cn"
    ldap_user_search_filter: str = "({username_attr}={username})"
    ldap_timeout: int = Field(default=10, ge=1, le=300)
    ldap_admin_users: str = ""
    ldap_admin_groups: str = ""
    ldap_user_groups: str = ""
    ldap_recursive_groups: bool = False
    ldap_group_attr: str = "memberOf"


class KeycloakConfig(_CategoryConfig):
    """Keycloak/OIDC configuration.

    Field order drives the admin UI, so the discovery URL sits next to the realm
    it replaces.
    """

    keycloak_enabled: bool = False
    keycloak_server_url: str = ""
    keycloak_internal_url: str = ""
    #: Full ``.well-known/openid-configuration`` URL. When set, every endpoint and
    #: the issuer come from the provider's metadata and ``keycloak_realm`` is
    #: ignored. Endpoints used to be built by concatenating
    #: ``server_url + "/realms/" + realm + ...``, which is a Keycloak-only URL
    #: shape — Authentik and others 404 on it (issue #353).
    keycloak_discovery_url: str = ""
    #: Only used when no discovery URL is set.
    keycloak_realm: str = "opentranscribe"
    keycloak_client_id: str = ""
    keycloak_client_secret: str | None = None  # Sensitive
    keycloak_callback_url: str = ""
    keycloak_admin_role: str = "admin"
    #: Dotted path to the claim carrying group/role membership. Keycloak puts it
    #: in ``realm_access.roles``, which is why that is the default; Authentik and
    #: Okta use ``groups``, Entra ID uses ``roles``. Reading the Keycloak-only
    #: claim from another provider fails silently — everyone logs in, nobody is
    #: an admin.
    keycloak_roles_claim: str = "realm_access.roles"
    #: Optional issuer override. Normally taken from the discovery document.
    keycloak_issuer: str = ""
    keycloak_scopes: str = "openid email profile"
    keycloak_timeout: int = Field(default=30, ge=1, le=300)
    #: True matches ``config.py:KEYCLOAK_VERIFY_AUDIENCE``. Both this and
    #: ``keycloak_verify_issuer`` are token-validation controls: an unparseable
    #: value must never be read as "off" (see ``AuthConfigService._convert_value``).
    keycloak_verify_audience: bool = True
    keycloak_audience: str = ""
    keycloak_use_pkce: bool = True
    keycloak_verify_issuer: bool = True


class PKIConfig(_CategoryConfig):
    """PKI/X.509 certificate configuration."""

    pki_enabled: bool = False
    pki_ca_cert_path: str = ""
    pki_verify_revocation: bool = False
    pki_cert_header: str = "X-Client-Cert"
    pki_cert_dn_header: str = "X-Client-Cert-DN"
    pki_admin_dns: str = ""
    pki_ocsp_timeout_seconds: int = Field(default=5, ge=1, le=120)
    pki_crl_cache_seconds: int = Field(default=3600, ge=1, le=604800)
    #: False, not True: ``config.py`` only soft-fails in a relaxed environment, so
    #: the deployed default is strict revocation checking.
    pki_revocation_soft_fail: bool = False
    pki_trusted_proxies: str = ""
    pki_mode: Literal["direct", "keycloak", "hybrid"] = "direct"
    pki_allow_password_fallback: bool = True
    pki_support_cac: bool = True
    pki_support_piv: bool = True


class PasswordPolicyConfig(_CategoryConfig):
    """Password policy configuration."""

    password_policy_enabled: bool = True
    password_min_length: int = Field(default=12, ge=8, le=128)
    password_require_uppercase: bool = True
    password_require_lowercase: bool = True
    password_require_digit: bool = True
    password_require_special: bool = True
    #: 0 disables reuse checking; FedRAMP IA-5 requires 24.
    password_history_count: int = Field(default=24, ge=0, le=100)
    #: 0 means passwords never expire.
    password_max_age_days: int = Field(default=60, ge=0, le=3650)


class MFAConfig(_CategoryConfig):
    """Multi-factor authentication configuration."""

    mfa_enabled: bool = False
    mfa_required: bool = False
    mfa_issuer_name: str = "OpenTranscribe"
    mfa_backup_code_count: int = Field(default=10, ge=1, le=50)
    mfa_token_expire_minutes: int = Field(default=5, ge=1, le=60)


class SessionConfig(_CategoryConfig):
    """Session and token configuration."""

    jwt_access_token_expire_minutes: int = Field(default=60, ge=1, le=1440)
    jwt_refresh_token_expire_days: int = Field(default=7, ge=1, le=365)
    session_idle_timeout_minutes: int = Field(default=15, ge=1, le=1440)
    session_absolute_timeout_minutes: int = Field(default=480, ge=1, le=10080)
    #: 0 = unlimited, matching ``config.py:MAX_CONCURRENT_SESSIONS``.
    max_concurrent_sessions: int = Field(default=5, ge=0, le=1000)
    concurrent_session_policy: Literal["terminate_oldest", "reject"] = "terminate_oldest"


class LoginBannerConfig(_CategoryConfig):
    """Login banner configuration (FedRAMP AC-8)."""

    login_banner_enabled: bool = False
    login_banner_text: str = Field(default="", max_length=10000)
    login_banner_classification: str = Field(default="UNCLASSIFIED", max_length=255)


class LockoutConfig(_CategoryConfig):
    """Account lockout and authentication rate-limit configuration."""

    account_lockout_enabled: bool = True
    account_lockout_threshold: int = Field(default=5, ge=1, le=1000)
    account_lockout_duration_minutes: int = Field(default=15, ge=1, le=10080)
    account_lockout_progressive: bool = True
    account_lockout_max_duration_minutes: int = Field(default=1440, ge=1, le=525600)
    rate_limit_enabled: bool = True
    rate_limit_auth_per_minute: int = Field(default=10, ge=1, le=10000)


#: Category -> model. Iteration order is the admin UI's tab order and is what
#: ``AuthConfigService.CONFIG_CATEGORIES`` and the endpoint's category allow-list
#: are both built from, so there is exactly one place to add a category.
CATEGORY_SCHEMAS: dict[str, type[_CategoryConfig]] = {
    "local": LocalAuthConfig,
    "ldap": LDAPConfig,
    "keycloak": KeycloakConfig,
    "pki": PKIConfig,
    "password_policy": PasswordPolicyConfig,
    "mfa": MFAConfig,
    "session": SessionConfig,
    "banner": LoginBannerConfig,
    "lockout": LockoutConfig,
}

#: Mirrors ``auth_config.config_key`` (``String(100)``). A longer key used to reach
#: Postgres and come back as a ``DataError`` wrapped in a generic 500.
MAX_CONFIG_KEY_LENGTH = 100

_FIELD_DEFAULTS: dict[str, Any] = {}


def coded_default(config_key: str, fallback: Any = None) -> Any:
    """Return the schema default for *config_key*.

    Used by the read path when a stored value cannot be parsed: falling back to
    the declared default keeps a security control (issuer/audience validation,
    LDAP TLS) at its safe setting instead of at ``False``, which is what a bare
    ``value.lower() in ("true", ...)`` produces for any garbage string.

    Args:
        config_key: Configuration key.
        fallback: Returned when the key belongs to no category schema.

    Returns:
        The declared default, or *fallback* for an unknown key.
    """
    if not _FIELD_DEFAULTS:
        for model in CATEGORY_SCHEMAS.values():
            for name, field in model.model_fields.items():
                _FIELD_DEFAULTS[name] = field.get_default(call_default_factory=True)
    return _FIELD_DEFAULTS.get(config_key, fallback)


#: Keys a category's cross-field rules need to see even when the payload does not
#: carry them. The caller loads these from the current effective config and merges
#: the payload over them — checking the payload alone would let an admin reach the
#: rejected state by saving one field at a time.
CROSS_FIELD_KEYS: dict[str, tuple[str, ...]] = {
    "local": ("local_enabled", "allow_registration"),
}


def _check_cross_field_rules(
    category: str, merged: dict[str, Any], incoming_keys: frozenset[str]
) -> None:
    """Reject combinations that are individually valid but jointly incoherent.

    Args:
        category: Configuration category being written.
        merged: Resulting state — current effective values with the payload applied.
        incoming_keys: Keys the request actually carried, used to word the error
            around the change the admin is making.

    Raises:
        ValueError: The resulting state is rejected.
    """
    if category != "local":
        return

    allow_registration = merged.get("allow_registration", coded_default("allow_registration"))
    local_enabled = merged.get("local_enabled", coded_default("local_enabled"))

    if not allow_registration or local_enabled:
        return

    # Self-registration hardcodes auth_type='local' plus a local password
    # (api/endpoints/auth/registration.py), so with local password login off every
    # account it mints is one that can never sign in.
    if "local_enabled" in incoming_keys and "allow_registration" not in incoming_keys:
        raise ValueError(
            "Cannot disable local password login (local_enabled) while self-registration "
            "(allow_registration) is enabled: registration creates local-password accounts "
            "that could never sign in. Turn off allow_registration first."
        )
    raise ValueError(
        "allow_registration cannot be enabled while local password login (local_enabled) is "
        "disabled: self-registration creates local-password accounts that could never sign in. "
        "Enable local_enabled first, or leave self-registration off."
    )


def _format_validation_error(category: str, exc: ValidationError) -> str:
    """Render a pydantic error as a message safe to return to the admin UI."""
    problems = []
    for error in exc.errors():
        key = ".".join(str(part) for part in error["loc"]) or "(payload)"
        problems.append(f"{key}: {error['msg']}")
    return f"Invalid {category} configuration — " + "; ".join(problems)


def validate_category_config(
    category: str,
    config: dict[str, Any],
    current: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate a partial category payload and return it type-coerced.

    Only the keys present in *config* are returned, so a save of one field never
    rewrites the rest of the tab with defaults.

    Args:
        category: Configuration category the payload is being written to.
        config: Raw key/value pairs from the request body.
        current: Currently effective values for ``CROSS_FIELD_KEYS[category]``.
            The payload is merged over these before the cross-field rules run,
            so a rejected combination cannot be assembled one save at a time.

    Returns:
        The same keys with values coerced to their declared types.

    Raises:
        ValueError: Unknown category, over-long/unknown key, unparseable value,
            a value outside the declared bounds, or a rejected combination.
            Callers at the HTTP edge turn this into a 400 — never a 500 and never
            a silent write.
    """
    model = CATEGORY_SCHEMAS.get(category)
    if model is None:
        raise ValueError(
            f"Unknown configuration category '{category}'. "
            f"Must be one of: {', '.join(CATEGORY_SCHEMAS)}"
        )

    if not isinstance(config, dict):
        raise ValueError("Configuration payload must be a JSON object")

    over_long = sorted(key for key in config if len(key) > MAX_CONFIG_KEY_LENGTH)
    if over_long:
        truncated = ", ".join(f"{key[:32]}… ({len(key)} chars)" for key in over_long)
        raise ValueError(
            f"Configuration key(s) exceed the {MAX_CONFIG_KEY_LENGTH}-character limit: {truncated}"
        )

    unknown = sorted(set(config) - set(model.model_fields))
    if unknown:
        raise ValueError(
            f"Unknown configuration key(s) for category '{category}': {', '.join(unknown)}. "
            f"Valid keys: {', '.join(model.model_fields)}"
        )

    try:
        validated = model.model_validate(config)
    except ValidationError as exc:
        raise ValueError(_format_validation_error(category, exc)) from exc

    cleaned = {key: getattr(validated, key) for key in config}
    _check_cross_field_rules(category, {**(current or {}), **cleaned}, frozenset(config))
    return cleaned


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
