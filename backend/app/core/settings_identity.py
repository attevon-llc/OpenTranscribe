"""Identity-provider settings: LDAP, OIDC, SAML, MFA and PKI.

Split out of ``app.core.config`` (v0.7.0 L07) as one cohesive concern, following the
``settings_*`` mixin pattern. ``Settings`` inherits these fields, so ``settings.<NAME>`` and
the environment variable names are unchanged. The env source, ``env_ignore_empty`` and
``extra="ignore"`` come from ``Settings``' own ``model_config``; this class is never
instantiated on its own.
"""

import os

from pydantic_settings import BaseSettings

from app.core.env import env_bool
from app.core.legacy_auth_env import oidc_bool_env
from app.core.legacy_auth_env import oidc_env
from app.core.legacy_auth_env import oidc_int_env


class IdentitySettings(BaseSettings):
    # ===== LDAP / Active Directory (seed values; the admin UI's stored keys win) =====
    LDAP_ENABLED: bool = False
    LDAP_SERVER: str = ""  # host name, or an ldap:// / ldaps:// URL (the scheme wins)
    LDAP_PORT: int = 636
    LDAP_USE_SSL: bool = True
    LDAP_USE_TLS: bool = False  # StartTLS on port 389; exclusive with LDAP_USE_SSL (LDAPS)
    LDAP_CA_CERT_FILE: str = ""  # PEM bundle of a private CA; replaces the system CAs
    LDAP_TLS_SERVER_NAME: str = ""  # name the certificate must carry; empty = LDAP_SERVER
    LDAP_TLS_SKIP_VERIFY: bool = False  # insecure: certificate not checked; warns per connection
    LDAP_BIND_DN: str = ""  # read-only service account used for searches
    LDAP_BIND_PASSWORD: str = ""
    LDAP_SEARCH_BASE: str = ""
    LDAP_USERNAME_ATTR: str = "sAMAccountName"  # uid for OpenLDAP / LLDAP
    # Plain field default (not a class-body expression computed from another
    # os.getenv call) resolved in `validate_auth_settings` below — same bug class
    # as MFA_REQUIRE_REDIS/DATABASE_URL: the old class-body `.replace(...)` chain
    # ran once at class-definition time against `os.getenv`, so it never saw a
    # value that pydantic-settings later sourced from a real .env FILE.
    LDAP_USER_SEARCH_FILTER: str = "({username_attr}={username})"
    LDAP_EMAIL_ATTR: str = "mail"
    LDAP_NAME_ATTR: str = "cn"
    LDAP_TIMEOUT: int = 10  # seconds; bounds the TCP connect and every read
    LDAP_ADMIN_USERS: str = ""  # comma-separated username-attribute values, any case
    # LDAP Group-based RBAC (alternative to LDAP_ADMIN_USERS)
    # Semicolon-separated group DNs that grant the admin role (a DN contains commas)
    LDAP_ADMIN_GROUPS: str = os.getenv("LDAP_ADMIN_GROUPS", "")
    # Semicolon-separated group DNs required to log in (empty = allow all)
    LDAP_USER_GROUPS: str = os.getenv("LDAP_USER_GROUPS", "")
    LDAP_RECURSIVE_GROUPS: bool = False  # nested groups via LDAP_MATCHING_RULE_IN_CHAIN
    # Attribute to check for group membership (default: memberOf for AD)
    LDAP_GROUP_ATTR: str = os.getenv("LDAP_GROUP_ATTR", "memberOf")

    # ===== OpenID Connect =====
    # Every one of these also resolves from its historical vendor-prefixed spelling,
    # which takes precedence when both are set — see core/legacy_auth_env.py, the one
    # module that still names it. Deployments never have to edit their .env.
    OIDC_ENABLED: bool = oidc_bool_env("OIDC_ENABLED", False)
    OIDC_SERVER_URL: str = oidc_env("OIDC_SERVER_URL")  # e.g., http://localhost:8180
    # Internal URL for backend-to-provider communication (Docker networking).
    # If not set, falls back to OIDC_SERVER_URL.
    OIDC_INTERNAL_URL: str = oidc_env("OIDC_INTERNAL_URL")
    OIDC_REALM: str = oidc_env("OIDC_REALM", "opentranscribe")
    OIDC_CLIENT_ID: str = oidc_env("OIDC_CLIENT_ID")
    OIDC_CLIENT_SECRET: str = oidc_env("OIDC_CLIENT_SECRET")
    # e.g. http://localhost:5173/login — the SPA route, not a backend path.
    OIDC_CALLBACK_URL: str = oidc_env("OIDC_CALLBACK_URL")
    # Role/group value in the token that grants admin access.
    OIDC_ADMIN_ROLE: str = oidc_env("OIDC_ADMIN_ROLE", "admin")
    OIDC_TIMEOUT: int = oidc_int_env("OIDC_TIMEOUT", 30)
    # OIDC Security: Enable audience (aud) claim validation (OWASP recommended)
    # Default to True for security - validates tokens are intended for this client
    OIDC_VERIFY_AUDIENCE: bool = oidc_bool_env("OIDC_VERIFY_AUDIENCE", True)
    # Expected audience claim value (usually the client ID)
    OIDC_AUDIENCE: str = oidc_env("OIDC_AUDIENCE")
    # Enable PKCE (Proof Key for Code Exchange) for OAuth 2.1 compliance
    OIDC_USE_PKCE: bool = oidc_bool_env("OIDC_USE_PKCE", True)
    # Enable issuer (iss) claim validation (OWASP recommended)
    OIDC_VERIFY_ISSUER: bool = oidc_bool_env("OIDC_VERIFY_ISSUER", True)

    # ===== Generic OIDC discovery (issue #353) =====
    # Without a discovery URL the endpoints are built from OIDC_SERVER_URL +
    # "/realms/<realm>/...", which is a single vendor's URL shape — Authentik and most
    # other providers 404 on it. Set a discovery URL and every endpoint (plus the
    # issuer) is read from the provider's own metadata document instead; the realm
    # form stays as the fallback so existing realm-based deployments are unaffected.
    OIDC_DISCOVERY_URL: str = oidc_env("OIDC_DISCOVERY_URL")
    OIDC_ISSUER: str = oidc_env("OIDC_ISSUER")
    # Dotted path to the claim carrying group/role membership. Realm-shaped
    # providers: realm_access.roles (the default). Authentik/Okta: groups. Entra ID:
    # roles. Getting this wrong is silent — everyone logs in and nobody is an admin.
    OIDC_ROLES_CLAIM: str = oidc_env("OIDC_ROLES_CLAIM", "realm_access.roles")
    OIDC_SCOPES: str = oidc_env("OIDC_SCOPES", "openid email profile")

    # ===== OIDC admission control =====
    # Semicolon-delimited group/role values read from OIDC_ROLES_CLAIM, mirroring
    # LDAP_USER_GROUPS. EMPTY ALLOW-LIST ADMITS EVERYONE — that is today's behaviour
    # and the upgrade-safe default, not an oversight. Without it, pointing this at a
    # corporate tenant gives every identity in the directory an account on first
    # login (auth/oidc/admission.py). Blocked wins over allowed.
    #
    # Plain os.getenv, not oidc_env: these names are new, so there is no retired
    # spelling to honour (core/legacy_auth_env.py owns that translation and its
    # alias map is the closed list of variables that ever had one).
    OIDC_ALLOWED_GROUPS: str = os.getenv("OIDC_ALLOWED_GROUPS", "")
    OIDC_BLOCKED_GROUPS: str = os.getenv("OIDC_BLOCKED_GROUPS", "")

    # ===== SAML 2.0 (#35) =====
    # No retired spelling to honour here (this is the auth type's only name), so
    # plain os.getenv throughout — unlike OIDC_*, none of these route through
    # core/legacy_auth_env.py.
    SAML_ENABLED: bool = env_bool("SAML_ENABLED", False)
    # Entity ID this SP identifies itself as. Also the default audience the IdP's
    # assertion must be addressed to.
    SAML_SP_ENTITY_ID: str = os.getenv("SAML_SP_ENTITY_ID", "")
    # e.g. https://localhost:5173/api/auth/saml/acs — must match exactly what is
    # registered with the IdP, or the assertion's Recipient/Destination check fails.
    SAML_SP_ACS_URL: str = os.getenv("SAML_SP_ACS_URL", "")
    SAML_SP_SLS_URL: str = os.getenv("SAML_SP_SLS_URL", "")
    # SP signing/encryption key pair, PEM. Only required when
    # SAML_SIGN_AUTHN_REQUESTS or SAML_WANT_ASSERTIONS_ENCRYPTED is on. Blank means
    # this SP requests but does not itself sign or decrypt.
    SAML_SP_X509_CERT: str = os.getenv("SAML_SP_X509_CERT", "")
    SAML_SP_PRIVATE_KEY: str = os.getenv("SAML_SP_PRIVATE_KEY", "")
    SAML_IDP_ENTITY_ID: str = os.getenv("SAML_IDP_ENTITY_ID", "")
    SAML_IDP_SSO_URL: str = os.getenv("SAML_IDP_SSO_URL", "")
    SAML_IDP_SLO_URL: str = os.getenv("SAML_IDP_SLO_URL", "")
    # The IdP's signing certificate, PEM (no BEGIN/END lines required — sp.py
    # strips them either way). This is what makes assertion verification real:
    # python3-saml (never hand-rolled parsing) checks the assertion's signature
    # against this, so an unset value must refuse to start SAML, not fall open.
    SAML_IDP_X509_CERT: str = os.getenv("SAML_IDP_X509_CERT", "")
    # OWASP-recommended posture: the IdP must sign what it asserts.
    SAML_WANT_ASSERTIONS_SIGNED: bool = env_bool("SAML_WANT_ASSERTIONS_SIGNED", True)
    SAML_WANT_MESSAGES_SIGNED: bool = env_bool("SAML_WANT_MESSAGES_SIGNED", True)
    SAML_SIGN_AUTHN_REQUESTS: bool = env_bool("SAML_SIGN_AUTHN_REQUESTS", False)
    # Attribute names to read from the assertion. No standard covers this across
    # IdPs — ADFS, Okta, and Azure AD SAML each ship different defaults — so these
    # are configurable rather than hardcoded to one vendor's claim URIs, same
    # reasoning as OIDC_ROLES_CLAIM.
    SAML_EMAIL_ATTRIBUTE: str = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress"
    SAML_NAME_ATTRIBUTE: str = os.getenv(
        "SAML_NAME_ATTRIBUTE", "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name"
    )
    SAML_GROUPS_ATTRIBUTE: str = os.getenv("SAML_GROUPS_ATTRIBUTE", "groups")
    SAML_ADMIN_GROUP: str = os.getenv("SAML_ADMIN_GROUP", "")
    # Same semantics and same upgrade-safe empty-admits-everyone default as
    # OIDC_ALLOWED_GROUPS/OIDC_BLOCKED_GROUPS.
    SAML_ALLOWED_GROUPS: str = os.getenv("SAML_ALLOWED_GROUPS", "")
    SAML_BLOCKED_GROUPS: str = os.getenv("SAML_BLOCKED_GROUPS", "")

    # ===== MFA Settings (FedRAMP IA-2) =====
    # MFA is disabled by default for air-gapped deployments
    MFA_ENABLED: bool = env_bool("MFA_ENABLED", False)
    # When MFA_REQUIRED is true, users must set up MFA on first login
    MFA_REQUIRED: bool = env_bool("MFA_REQUIRED", False)
    # Require admins (role "admin") to enrol and use MFA even when MFA_REQUIRED is false.
    # Recommended true wherever MFA_ENABLED is true; defaults false so an upgrade cannot lock
    # an existing admin out before they have enrolled. Has no effect while MFA_ENABLED is false.
    MFA_REQUIRED_FOR_ADMINS: bool = False
    # Issuer name shown in authenticator apps
    MFA_ISSUER_NAME: str = os.getenv("MFA_ISSUER_NAME", "OpenTranscribe")
    # Number of backup codes to generate (one-time use)
    MFA_BACKUP_CODE_COUNT: int = 10
    # MFA token expiry in minutes (short-lived token for MFA verification step)
    MFA_TOKEN_EXPIRE_MINUTES: int = 5
    # TOTP verification window (number of time steps before/after to accept)
    # 1 = allow 1 step before/after for clock drift (±30 seconds)
    TOTP_VALID_WINDOW: int = 1
    # Require Redis for MFA replay protection (fail-secure mode).
    # Redis is the only place the "this TOTP code / this MFA half-token was already
    # used" claim lives. With this off, a Redis outage silently downgrades MFA to
    # replayable: _consume_totp_code accepts a replayed code and the half-token
    # blacklist check answers "not used". Defaulting it off therefore made the whole
    # replay defence fail OPEN in production.
    # Default follows the hardened posture (never ENVIRONMENT == "production" —
    # see app/core/CLAUDE.md): fail closed in a real deployment, stay permissive in
    # dev/test where a stack without Redis must still log in. An explicit env value
    # always wins over the default.
    #
    # Plain `bool` field (not `bool | None` — that shape fights every downstream
    # reader that expects `bool`, e.g. `DynamicAuthSettings.get_bool`'s signature),
    # resolved in `validate_auth_settings` below (keyed on the INSTANCE's
    # ENVIRONMENT), not a class-body `os.getenv(...)` expression: the class-body
    # form computed its conditional default via `os.getenv(key, computed_default)`,
    # but `os.getenv` only substitutes its default when the var is UNSET — a var
    # set to the EMPTY string ("" — `.env.example`'s "use the coded default"
    # convention) previously still returned "" and skipped the conditional default
    # entirely, silently landing on False regardless of environment. Now `bool | None`:
    # `None` unambiguously means "unset", so `validate_auth_settings` can distinguish
    # that from an explicit `false` and only recompute the environment-conditional
    # default when the field is still `None`, using `is_relaxed_environment(self.ENVIRONMENT)`.
    MFA_REQUIRE_REDIS: bool | None = None

    # ===== PKI/X.509 Certificate Configuration =====
    PKI_ENABLED: bool = env_bool("PKI_ENABLED", False)
    PKI_CA_CERT_PATH: str = os.getenv(
        "PKI_CA_CERT_PATH", ""
    )  # Path to CA certificate for validation
    PKI_VERIFY_REVOCATION: bool = env_bool("PKI_VERIFY_REVOCATION", False)  # Check CRL/OCSP
    PKI_CERT_HEADER: str = os.getenv(
        "PKI_CERT_HEADER", "X-Client-Cert"
    )  # Header name from reverse proxy
    PKI_CERT_DN_HEADER: str = os.getenv(
        "PKI_CERT_DN_HEADER", "X-Client-Cert-DN"
    )  # Distinguished Name header
    PKI_ADMIN_DNS: str = os.getenv(
        "PKI_ADMIN_DNS", ""
    )  # SEMICOLON-separated admin certificate DNs (a DN contains commas)
    # OCSP/CRL revocation checking settings
    PKI_OCSP_TIMEOUT_SECONDS: int = 5
    PKI_CRL_CACHE_SECONDS: int = 3600  # Cache CRL for 1 hour
    # Soft-fail allows authentication if revocation check fails (network issues)
    # Defaults to false in production (strict revocation checking). `bool | None`
    # field, resolved in `validate_auth_settings` below — see MFA_REQUIRE_REDIS
    # above for why this can't be a class-body `os.getenv(key, computed_default)`
    # expression, and why `None` (not a literal `False`) is what marks it unset.
    PKI_REVOCATION_SOFT_FAIL: bool | None = None
    # Maximum cache size for OCSP responses (LRU eviction when exceeded)
    PKI_OCSP_CACHE_MAX_SIZE: int = 1000
    # Maximum cache size for CRLs (LRU eviction when exceeded)
    PKI_CRL_CACHE_MAX_SIZE: int = 1000
    # Trusted proxy IPs for PKI certificate headers (comma-separated)
    # Only accept PKI certificate headers from these IPs
    # Example: "127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
    PKI_TRUSTED_PROXIES: str = os.getenv("PKI_TRUSTED_PROXIES", "")
