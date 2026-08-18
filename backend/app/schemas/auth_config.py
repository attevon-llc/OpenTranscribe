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

    ``extra="forbid"`` is what turns a typo'd key (``oidc_verify_audiance``)
    into a 400 instead of a row that is stored forever and read by nothing.
    """

    model_config = ConfigDict(extra="forbid")


class LocalAuthConfig(_CategoryConfig):
    """Local (password) identity-source configuration.

    The password-policy, MFA and lockout keys the admin UI's Local tab also
    renders belong to ``password_policy`` / ``mfa`` / ``lockout`` — see the
    uniqueness note above. Four aliases of those keys used to be declared here
    as well (``mfa_issuer``, ``password_require_numbers``, ``max_login_attempts``,
    ``lockout_duration_minutes``); each was an orphan spelling of a real key
    (``mfa_issuer_name``, ``password_require_digit``,
    ``account_lockout_threshold``, ``account_lockout_duration_minutes``) that no
    code ever read. The panel stopped sending them and the API kept accepting
    them, so a save still returned 200. They are deleted rather than bridged:
    two writable spellings of one control is how the two disagree.
    ``AuthConfigService.RETIRED_KEYS`` keeps any row an older panel wrote from
    being served back.
    """

    local_enabled: bool = True
    allow_registration: bool = True
    require_email_verification: bool = False
    #: Admin admission control for *newly provisioned* accounts, wherever they come
    #: from: self-registration and every external-IdP JIT path. It lives in this
    #: category because ``allow_registration`` is its nearest neighbour — the two
    #: together are "who may get an account here" — not because it is local-only.
    #:
    #: False keeps the pre-existing behaviour (new accounts are usable
    #: immediately), which is what makes turning it on an opt-in rather than an
    #: upgrade that strands every pending signup.
    require_account_approval: bool = False


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


class OIDCConfig(_CategoryConfig):
    """OpenID Connect configuration.

    Field order drives the admin UI, so the discovery URL sits next to the realm
    it replaces.
    """

    oidc_enabled: bool = False
    oidc_server_url: str = ""
    oidc_internal_url: str = ""
    #: Full ``.well-known/openid-configuration`` URL. When set, every endpoint and
    #: the issuer come from the provider's metadata and ``oidc_realm`` is ignored.
    #: Endpoints used to be built by concatenating
    #: ``server_url + "/realms/" + realm + ...``, which is one vendor's URL shape —
    #: Authentik and others 404 on it (issue #353).
    oidc_discovery_url: str = ""
    #: Only used when no discovery URL is set.
    oidc_realm: str = "opentranscribe"
    oidc_client_id: str = ""
    oidc_client_secret: str | None = None  # Sensitive
    oidc_callback_url: str = ""
    oidc_admin_role: str = "admin"
    #: Dotted path to the claim carrying group/role membership. Realm-shaped
    #: providers put it in ``realm_access.roles``, which is why that is the default;
    #: Authentik and Okta use ``groups``, Entra ID uses ``roles``. Reading the wrong
    #: claim fails silently — everyone logs in, nobody is an admin.
    oidc_roles_claim: str = "realm_access.roles"
    #: Optional issuer override. Normally taken from the discovery document.
    oidc_issuer: str = ""
    oidc_scopes: str = "openid email profile"
    #: Semicolon-delimited values read from ``oidc_roles_claim`` that a login must
    #: carry at least one of. **Empty admits everyone** — that is what JIT
    #: provisioning did unconditionally before this existed, so an empty list is
    #: the upgrade-safe default rather than a lockout. LDAP has had the equivalent
    #: (``ldap_user_groups``) since it shipped.
    oidc_allowed_groups: str = ""
    #: Values that DENY access outright, evaluated before the allow-list. "Blocked"
    #: means refused, not "exempt from the allow-list": a deployment needs a way to
    #: keep a contractor group out of a tenant it otherwise admits wholesale.
    oidc_blocked_groups: str = ""
    oidc_timeout: int = Field(default=30, ge=1, le=300)
    #: True matches ``config.py:OIDC_VERIFY_AUDIENCE``. Both this and
    #: ``oidc_verify_issuer`` are token-validation controls: an unparseable value
    #: must never be read as "off" (see ``AuthConfigService._convert_value``).
    oidc_verify_audience: bool = True
    oidc_audience: str = ""
    oidc_use_pkce: bool = True
    oidc_verify_issuer: bool = True


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
    #: How the certificate reaches us, which is the only PKI choice the backend
    #: actually makes (``auth/pki_auth.py``). ``header``: a trusted proxy
    #: terminates mTLS and forwards the certificate and/or its DN. ``mutual_tls``:
    #: same transport, but a bare DN assertion is refused — the full certificate
    #: must be forwarded so this application validates it itself.
    #:
    #: It was ``direct``/``broker``/``hybrid``, describing a delegation choice no
    #: code branched on, and sharing no value with the ``header``/``mutual_tls``
    #: the admin UI sends — so **every save of the PKI tab was rejected** once
    #: unknown values started 400ing.
    pki_mode: Literal["header", "mutual_tls"] = "header"
    #: Deployment ceiling over the per-user ``User.allow_local_fallback``:
    #: effective = per-user AND this. True keeps the per-user flag as the sole
    #: gate, which is the pre-existing behaviour; the per-user flag already
    #: defaults to False, so defaulting this to False instead would silently
    #: revoke fallback a super_admin had granted.
    pki_allow_password_fallback: bool = True

    # `pki_support_cac` / `pki_support_piv` were removed in v375: they gated
    # nothing. `pki_auth.extract_display_name_from_gov_dn` parses both the DoD CAC
    # and the PIV CN formats for every certificate, unconditionally, and always
    # has. Their stored rows are deleted by the same revision.


class SAMLConfig(_CategoryConfig):
    """SAML 2.0 configuration (#35).

    Field order mirrors ``OIDCConfig``: enable flag, SP identity, IdP identity,
    security posture, attribute mapping, then admission.
    """

    saml_enabled: bool = False
    saml_sp_entity_id: str = ""
    saml_sp_acs_url: str = ""
    saml_sp_sls_url: str = ""
    #: Public — safe to display. Only required when signing/encryption is on.
    saml_sp_x509_cert: str = ""
    saml_sp_private_key: str | None = None  # Sensitive
    saml_idp_entity_id: str = ""
    saml_idp_sso_url: str = ""
    saml_idp_slo_url: str = ""
    #: The IdP's own signing certificate — what makes assertion signature
    #: verification real. Not sensitive (it is public key material the IdP itself
    #: publishes), but required before SAML can be enabled at all.
    saml_idp_x509_cert: str = ""
    saml_want_assertions_signed: bool = True
    saml_want_messages_signed: bool = True
    saml_sign_authn_requests: bool = False
    saml_email_attribute: str = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress"
    saml_name_attribute: str = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name"
    saml_groups_attribute: str = "groups"
    saml_admin_group: str = ""
    #: Same empty-admits-everyone semantics as ``OIDCConfig.oidc_allowed_groups``.
    saml_allowed_groups: str = ""
    saml_blocked_groups: str = ""


class ProxyAuthConfig(_CategoryConfig):
    """Trusted-header (reverse-proxy) configuration — ``auth_type='proxy'``.

    An authenticating proxy asserts the identity; this tab decides whether to
    believe it. The two fields that carry the whole security story are
    ``proxy_trusted_proxies`` (empty = refuse everything) and ``proxy_role_header``
    (empty = the proxy grants no privilege at all).
    """

    proxy_enabled: bool = False
    #: Comma-separated IPs/CIDRs. **Empty refuses every header-sourced assertion**,
    #: and ``main.py`` refuses to boot hardened with ``proxy_enabled`` and no
    #: allowlist — the same shape as the PKI guard. Open WebUI's equivalent trusts
    #: the network and nothing else; this is the deliberate divergence.
    proxy_trusted_proxies: str = ""
    proxy_email_header: str = "X-Forwarded-Email"
    proxy_name_header: str = "X-Forwarded-User"
    #: No default. A groups header drives in-app group membership through the shared
    #: IdP reconciler, so reading one nobody configured would let a proxy that
    #: happens to forward ``X-Forwarded-Groups`` start granting groups silently.
    proxy_groups_header: str = ""
    proxy_groups_separator: str = ","
    #: Opt-in, and capped at ``admin``: ``super_admin`` is unreachable through it,
    #: consistent with every other external identity source here.
    proxy_role_header: str = ""
    #: Sensitive. Constant-time compared, so an allowlisted proxy that has been
    #: misconfigured to pass client headers through is not by itself takeover.
    proxy_shared_secret: str | None = None
    #: Email-domain admission list. Empty admits everyone — the upgrade-safe reading
    #: ``oidc_allowed_groups`` uses.
    proxy_allowed_domains: str = ""
    proxy_jit_provisioning: bool = True


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
    """Session and token configuration.

    A session is a ``refresh_token`` row. The three live settings here are
    enforced against those rows — the timeouts in
    ``token_service.verify_refresh_token``, the limit in
    ``api/endpoints/auth/login.py`` — and take effect without a restart.
    """

    #: Restart-required (``AuthConfigService.RESTART_REQUIRED_KEYS``):
    #: ``auth/cookies.py`` computes cookie max-age from these two at import time,
    #: so a live change would leave cookie and token lifetimes disagreeing.
    jwt_access_token_expire_minutes: int = Field(default=60, ge=1, le=1440)
    jwt_refresh_token_expire_days: int = Field(default=7, ge=1, le=365)
    #: Enforced when a refresh token is presented, so the granularity is one
    #: access-token lifetime — see the note on ``verify_refresh_token`` for why
    #: per-request tracking is the wrong control. The admin UI cannot set 0; the
    #: enforcement path still treats 0 as "disabled" because
    #: ``SESSION_IDLE_TIMEOUT_MINUTES`` comes from an unbounded ``.env`` value.
    session_idle_timeout_minutes: int = Field(default=15, ge=1, le=1440)
    session_absolute_timeout_minutes: int = Field(default=480, ge=1, le=10080)
    #: 0 = unlimited, matching ``config.py:MAX_CONCURRENT_SESSIONS``.
    max_concurrent_sessions: int = Field(default=5, ge=0, le=1000)
    #: These are the two strings ``login.py`` compares against. The admin panel
    #: offers ``oldest``/``newest``/``all``, none of which can ever match — so the
    #: limit silently enforced nothing whichever option was chosen. The backend
    #: vocabulary is the one with code behind it; the panel must send these.
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
    "oidc": OIDCConfig,
    "saml": SAMLConfig,
    "pki": PKIConfig,
    "proxy": ProxyAuthConfig,
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
    "pki": ("pki_enabled", "pki_verify_revocation", "pki_ca_cert_path"),
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
    if category == "pki":
        _check_pki_cross_field_rules(merged, incoming_keys)
        return

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


def _check_pki_cross_field_rules(merged: dict[str, Any], incoming_keys: frozenset[str]) -> None:
    """Refuse revocation checking with no CA bundle to build the issuer chain from.

    ``core/config.py:_validate_pki_settings`` has always raised this at startup for
    the ``.env`` spelling. It could not see the DB copy, which did not matter while
    ``pki_auth.py`` read ``settings.PKI_*`` directly and the DB keys were inert.
    Now that they are live (issue #498), the admin UI can assemble a state the
    environment refuses to boot with — so the same rule has to hold here.

    Without the CA bundle ``_load_issuer_certificate`` returns None, OCSP is
    skipped, the CRL cross-check has nothing to verify a signature against, and
    every certificate lands on the ``pki_revocation_soft_fail`` branch: silently
    admitted, or universally rejected. Both are worse than a 400 at save time.

    Raises:
        ValueError: Revocation checking is on with no CA certificate path.
    """
    if not merged.get("pki_enabled", coded_default("pki_enabled")):
        return
    if not merged.get("pki_verify_revocation", coded_default("pki_verify_revocation")):
        return
    if merged.get("pki_ca_cert_path", coded_default("pki_ca_cert_path")):
        return

    if "pki_verify_revocation" in incoming_keys:
        raise ValueError(
            "Cannot enable pki_verify_revocation without pki_ca_cert_path: revocation "
            "checking needs a CA certificate to build the issuer chain, and without one "
            "every certificate falls through to the pki_revocation_soft_fail decision. "
            "Set pki_ca_cert_path first."
        )
    raise ValueError(
        "pki_ca_cert_path cannot be cleared while pki_verify_revocation is enabled: "
        "revocation checking would have no CA certificate to verify against. Turn off "
        "pki_verify_revocation first, or supply a different CA path."
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

    category: str  # ldap or oidc
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
    oidc_enabled: bool = False
    pki_enabled: bool = False
    proxy_enabled: bool = False
    mfa_enabled: bool = False
    password_policy_enabled: bool = True
    login_banner_enabled: bool = False
