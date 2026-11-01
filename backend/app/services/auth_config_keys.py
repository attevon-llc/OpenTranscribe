"""Key tables for the DB-backed authentication configuration.

Which keys are secret, restart-bound, retired, how each is typed, which env var
backs it, and which category owns it. Read by ``AuthConfigService`` and by the
admin auth-config endpoint.
"""

from app.schemas.auth_config import CATEGORY_SCHEMAS

# Keys that contain sensitive data and should be encrypted
SENSITIVE_KEYS = {
    "ldap_bind_password",
    "oidc_client_secret",
    "proxy_shared_secret",
    "saml_sp_private_key",
}

#: Keys whose new value does NOT take effect until the process restarts.
#: Written to ``auth_config.requires_restart`` so the admin UI can say so
#: instead of implying the change is live. That column has existed since the
#: table was created and was never written by anything.
#:
#: A visible "requires restart" badge is an acceptable outcome. A control that
#: saves with a success toast and enforces nothing is not — so a key belongs
#: here only when the value is genuinely frozen before any request runs, and
#: the reason is recorded next to it. ``tests/unit/test_auth_config_has_readers``
#: enforces the membership in both directions.
#:
#: * ``jwt_*`` — read at **import time** by ``app/auth/cookies.py`` to compute
#:   ``ACCESS_MAX_AGE`` / ``REFRESH_MAX_AGE``. Honouring a live change for the
#:   token but not the cookie would end sessions early (cookie expires first)
#:   or leave a dead cookie behind (token expires first). Everything else in
#:   the session category is enforced against ``refresh_token`` rows at
#:   request time and is genuinely live.
#: * ``rate_limit_enabled`` — ``auth/rate_limit.py`` builds the module-level
#:   ``Limiter(enabled=settings.RATE_LIMIT_ENABLED)`` at import.
#: * ``rate_limit_auth_per_minute`` — every auth endpoint is decorated
#:   ``@limiter.limit(get_auth_rate_limit())``, and a decorator argument is
#:   evaluated once, at import. slowapi accepts a *callable* limit, so making
#:   this live is a real refactor (drop the call parentheses at ~20 decorator
#:   sites and read the effective value inside ``get_auth_rate_limit``), not a
#:   bridge — until then the badge is the truthful answer.
RESTART_REQUIRED_KEYS = frozenset(
    {
        "jwt_access_token_expire_minutes",
        "jwt_refresh_token_expire_days",
        "rate_limit_enabled",
        "rate_limit_auth_per_minute",
    }
)

#: Keys the API once accepted that are now removed from the category schemas.
#: Each was an orphan alias of a real key — ``mfa_issuer`` for
#: ``mfa_issuer_name``, ``password_require_numbers`` for
#: ``password_require_digit``, ``max_login_attempts`` for
#: ``account_lockout_threshold``, ``lockout_duration_minutes`` for
#: ``account_lockout_duration_minutes`` — written by an older admin panel and
#: read by nothing. Writes are now rejected by ``validate_category_config``
#: (they are unknown keys); this set additionally keeps a stale row from being
#: served back by ``get_config_by_category``, which filters by category and
#: would otherwise still hand the ``local`` tab a control that does nothing.
RETIRED_KEYS = frozenset(
    {
        "mfa_issuer",
        "password_require_numbers",
        "max_login_attempts",
        "lockout_duration_minutes",
    }
)

# Mapping of config keys to their data types
DATA_TYPE_MAPPING = {
    # Local auth settings
    "local_enabled": "bool",
    "allow_registration": "bool",
    "require_email_verification": "bool",
    "require_account_approval": "bool",
    # LDAP settings
    "ldap_enabled": "bool",
    "ldap_port": "int",
    "ldap_use_ssl": "bool",
    "ldap_use_tls": "bool",
    "ldap_timeout": "int",
    "ldap_recursive_groups": "bool",
    # OIDC settings
    "oidc_enabled": "bool",
    "oidc_timeout": "int",
    "oidc_verify_audience": "bool",
    "oidc_use_pkce": "bool",
    "oidc_verify_issuer": "bool",
    # SAML settings (C10)
    "saml_enabled": "bool",
    "saml_want_assertions_signed": "bool",
    "saml_want_messages_signed": "bool",
    "saml_sign_authn_requests": "bool",
    # PKI settings
    "pki_enabled": "bool",
    "pki_verify_revocation": "bool",
    "pki_ocsp_timeout_seconds": "int",
    "pki_crl_cache_seconds": "int",
    "pki_revocation_soft_fail": "bool",
    "pki_allow_password_fallback": "bool",
    "pki_mode": "string",
    # Trusted-header (reverse-proxy) settings
    "proxy_enabled": "bool",
    "proxy_jit_provisioning": "bool",
    # Password policy settings
    "password_policy_enabled": "bool",
    "password_policy_profile": "string",
    "password_max_length": "int",
    "password_min_length": "int",
    "password_require_uppercase": "bool",
    "password_require_lowercase": "bool",
    "password_require_digit": "bool",
    "password_require_special": "bool",
    "password_history_count": "int",
    "password_max_age_days": "int",
    "password_min_age_hours": "int",
    "password_blocklist_enabled": "string",
    "password_hibp_enabled": "bool",
    # MFA settings
    "mfa_enabled": "bool",
    "mfa_required": "bool",
    "mfa_required_for_admins": "bool",
    "mfa_backup_code_count": "int",
    "mfa_token_expire_minutes": "int",
    # Session settings
    "jwt_access_token_expire_minutes": "int",
    "jwt_refresh_token_expire_days": "int",
    "session_idle_timeout_minutes": "int",
    "session_absolute_timeout_minutes": "int",
    "max_concurrent_sessions": "int",
    "concurrent_session_policy": "string",
    # Login banner settings
    "login_banner_enabled": "bool",
    # Account settings
    "account_lockout_threshold": "int",
    "account_lockout_duration_minutes": "int",
    "account_lockout_progressive": "bool",
    "account_lockout_max_duration_minutes": "int",
    "account_lockout_enabled": "bool",
    "rate_limit_auth_per_minute": "int",
    "rate_limit_enabled": "bool",
}
# Environment variable to config key mapping
ENV_TO_CONFIG_MAPPING = {
    # Local / identity source. ALLOW_OPEN_REGISTRATION was missing here, which
    # is why the admin UI's self-registration toggle did nothing: the endpoint
    # read the env var and the DB key had no env counterpart to migrate from.
    "ALLOW_OPEN_REGISTRATION": "allow_registration",
    "LOCAL_AUTH_ENABLED": "local_enabled",
    "REQUIRE_ACCOUNT_APPROVAL": "require_account_approval",
    # LDAP
    "LDAP_ENABLED": "ldap_enabled",
    "LDAP_SERVER": "ldap_server",
    "LDAP_PORT": "ldap_port",
    "LDAP_USE_SSL": "ldap_use_ssl",
    "LDAP_USE_TLS": "ldap_use_tls",
    "LDAP_BIND_DN": "ldap_bind_dn",
    "LDAP_BIND_PASSWORD": "ldap_bind_password",
    "LDAP_SEARCH_BASE": "ldap_search_base",
    "LDAP_USERNAME_ATTR": "ldap_username_attr",
    "LDAP_EMAIL_ATTR": "ldap_email_attr",
    "LDAP_NAME_ATTR": "ldap_name_attr",
    "LDAP_TIMEOUT": "ldap_timeout",
    "LDAP_ADMIN_USERS": "ldap_admin_users",
    "LDAP_ADMIN_GROUPS": "ldap_admin_groups",
    "LDAP_USER_GROUPS": "ldap_user_groups",
    "LDAP_RECURSIVE_GROUPS": "ldap_recursive_groups",
    "LDAP_GROUP_ATTR": "ldap_group_attr",
    "LDAP_USER_SEARCH_FILTER": "ldap_user_search_filter",
    # OIDC. Only the canonical spellings appear here: every retired variable
    # name is translated onto its OIDC_* counterpart before Settings is built,
    # by core/legacy_auth_env.py. That is the one place the old spelling lives,
    # so `getattr(settings, env_var_for(key))` below resolves correctly for a
    # deployment that still sets the historical names, without this table having
    # to know they exist.
    "OIDC_ENABLED": "oidc_enabled",
    "OIDC_SERVER_URL": "oidc_server_url",
    "OIDC_INTERNAL_URL": "oidc_internal_url",
    "OIDC_DISCOVERY_URL": "oidc_discovery_url",
    "OIDC_ISSUER": "oidc_issuer",
    "OIDC_ROLES_CLAIM": "oidc_roles_claim",
    "OIDC_SCOPES": "oidc_scopes",
    "OIDC_ALLOWED_GROUPS": "oidc_allowed_groups",
    "OIDC_BLOCKED_GROUPS": "oidc_blocked_groups",
    "OIDC_REALM": "oidc_realm",
    "OIDC_CLIENT_ID": "oidc_client_id",
    "OIDC_CLIENT_SECRET": "oidc_client_secret",
    "OIDC_CALLBACK_URL": "oidc_callback_url",
    "OIDC_ADMIN_ROLE": "oidc_admin_role",
    "OIDC_TIMEOUT": "oidc_timeout",
    "OIDC_VERIFY_AUDIENCE": "oidc_verify_audience",
    "OIDC_AUDIENCE": "oidc_audience",
    "OIDC_USE_PKCE": "oidc_use_pkce",
    "OIDC_VERIFY_ISSUER": "oidc_verify_issuer",
    # SAML (C10, issue #567). These previously resolved by accident —
    # ``saml_enabled``.upper() happens to equal ``SAML_ENABLED`` for every key
    # here, so ``env_var_for()`` worked without being registered. That is not
    # true of every category (``allow_registration`` -> ``ALLOW_OPEN_REGISTRATION``
    # above is why this mapping exists at all), so an unregistered key silently
    # resolves to ``None`` the moment a future rename breaks the coincidence.
    "SAML_ENABLED": "saml_enabled",
    "SAML_SP_ENTITY_ID": "saml_sp_entity_id",
    "SAML_SP_ACS_URL": "saml_sp_acs_url",
    "SAML_SP_SLS_URL": "saml_sp_sls_url",
    "SAML_SP_X509_CERT": "saml_sp_x509_cert",
    "SAML_SP_PRIVATE_KEY": "saml_sp_private_key",
    "SAML_IDP_ENTITY_ID": "saml_idp_entity_id",
    "SAML_IDP_SSO_URL": "saml_idp_sso_url",
    "SAML_IDP_SLO_URL": "saml_idp_slo_url",
    "SAML_IDP_X509_CERT": "saml_idp_x509_cert",
    "SAML_WANT_ASSERTIONS_SIGNED": "saml_want_assertions_signed",
    "SAML_WANT_MESSAGES_SIGNED": "saml_want_messages_signed",
    "SAML_SIGN_AUTHN_REQUESTS": "saml_sign_authn_requests",
    "SAML_EMAIL_ATTRIBUTE": "saml_email_attribute",
    "SAML_NAME_ATTRIBUTE": "saml_name_attribute",
    "SAML_GROUPS_ATTRIBUTE": "saml_groups_attribute",
    "SAML_ADMIN_GROUP": "saml_admin_group",
    "SAML_ALLOWED_GROUPS": "saml_allowed_groups",
    "SAML_BLOCKED_GROUPS": "saml_blocked_groups",
    # PKI
    "PKI_ENABLED": "pki_enabled",
    "PKI_CA_CERT_PATH": "pki_ca_cert_path",
    "PKI_VERIFY_REVOCATION": "pki_verify_revocation",
    "PKI_CERT_HEADER": "pki_cert_header",
    "PKI_CERT_DN_HEADER": "pki_cert_dn_header",
    "PKI_ADMIN_DNS": "pki_admin_dns",
    "PKI_OCSP_TIMEOUT_SECONDS": "pki_ocsp_timeout_seconds",
    "PKI_CRL_CACHE_SECONDS": "pki_crl_cache_seconds",
    "PKI_REVOCATION_SOFT_FAIL": "pki_revocation_soft_fail",
    "PKI_TRUSTED_PROXIES": "pki_trusted_proxies",
    # Trusted-header (reverse proxy)
    "PROXY_ENABLED": "proxy_enabled",
    "PROXY_TRUSTED_PROXIES": "proxy_trusted_proxies",
    "PROXY_EMAIL_HEADER": "proxy_email_header",
    "PROXY_NAME_HEADER": "proxy_name_header",
    "PROXY_GROUPS_HEADER": "proxy_groups_header",
    "PROXY_GROUPS_SEPARATOR": "proxy_groups_separator",
    "PROXY_ROLE_HEADER": "proxy_role_header",
    "PROXY_SHARED_SECRET": "proxy_shared_secret",
    "PROXY_ALLOWED_DOMAINS": "proxy_allowed_domains",
    "PROXY_JIT_PROVISIONING": "proxy_jit_provisioning",
    # Password policy
    "PASSWORD_POLICY_ENABLED": "password_policy_enabled",
    "PASSWORD_POLICY_PROFILE": "password_policy_profile",
    "PASSWORD_MAX_LENGTH": "password_max_length",
    "PASSWORD_MIN_LENGTH": "password_min_length",
    "PASSWORD_REQUIRE_UPPERCASE": "password_require_uppercase",
    "PASSWORD_REQUIRE_LOWERCASE": "password_require_lowercase",
    "PASSWORD_REQUIRE_DIGIT": "password_require_digit",
    "PASSWORD_REQUIRE_SPECIAL": "password_require_special",
    "PASSWORD_HISTORY_COUNT": "password_history_count",
    "PASSWORD_MAX_AGE_DAYS": "password_max_age_days",
    "PASSWORD_MIN_AGE_HOURS": "password_min_age_hours",
    "PASSWORD_BLOCKLIST_ENABLED": "password_blocklist_enabled",
    "PASSWORD_HIBP_ENABLED": "password_hibp_enabled",
    # MFA
    "MFA_ENABLED": "mfa_enabled",
    "MFA_REQUIRED": "mfa_required",
    "MFA_REQUIRED_FOR_ADMINS": "mfa_required_for_admins",
    "MFA_ISSUER_NAME": "mfa_issuer_name",
    "MFA_BACKUP_CODE_COUNT": "mfa_backup_code_count",
    "MFA_TOKEN_EXPIRE_MINUTES": "mfa_token_expire_minutes",
    # Session
    "JWT_ACCESS_TOKEN_EXPIRE_MINUTES": "jwt_access_token_expire_minutes",
    "JWT_REFRESH_TOKEN_EXPIRE_DAYS": "jwt_refresh_token_expire_days",
    "SESSION_IDLE_TIMEOUT_MINUTES": "session_idle_timeout_minutes",
    "SESSION_ABSOLUTE_TIMEOUT_MINUTES": "session_absolute_timeout_minutes",
    "MAX_CONCURRENT_SESSIONS": "max_concurrent_sessions",
    "CONCURRENT_SESSION_POLICY": "concurrent_session_policy",
    # Login banner
    "LOGIN_BANNER_ENABLED": "login_banner_enabled",
    "LOGIN_BANNER_TEXT": "login_banner_text",
    "LOGIN_BANNER_CLASSIFICATION": "login_banner_classification",
    # Account lockout
    "ACCOUNT_LOCKOUT_THRESHOLD": "account_lockout_threshold",
    "ACCOUNT_LOCKOUT_DURATION_MINUTES": "account_lockout_duration_minutes",
    "ACCOUNT_LOCKOUT_PROGRESSIVE": "account_lockout_progressive",
    "ACCOUNT_LOCKOUT_MAX_DURATION_MINUTES": "account_lockout_max_duration_minutes",
    "ACCOUNT_LOCKOUT_ENABLED": "account_lockout_enabled",
    # Rate limiting
    "RATE_LIMIT_AUTH_PER_MINUTE": "rate_limit_auth_per_minute",
    "RATE_LIMIT_ENABLED": "rate_limit_enabled",
}

#: Config keys grouped by category, derived from the per-category Pydantic
#: models in ``app/schemas/auth_config.py``.
#:
#: It was a hand-maintained literal that drifted from those models and listed
#: eight keys under TWO categories each (the password-policy and MFA keys the
#: admin UI's Local tab also renders). ``auth_config.config_key`` is globally
#: UNIQUE, so such a key kept whichever category wrote it first and then went
#: missing from the other tab's ``GET /{category}``. One source of truth means
#: the duplicate cannot come back: a key lives in exactly one model.
CONFIG_CATEGORIES: dict[str, list[str]] = {
    category: list(model.model_fields) for category, model in CATEGORY_SCHEMAS.items()
}
