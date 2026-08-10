"""
Authentication type constants.

Centralizes all authentication type constants to avoid magic strings
and ensure consistency across the codebase.
"""

# Authentication type constants
AUTH_TYPE_LOCAL = "local"
AUTH_TYPE_LDAP = "ldap"
AUTH_TYPE_OIDC = "oidc"
AUTH_TYPE_PKI = "pki"
# Trusted-header authentication: an authenticating reverse proxy asserts the identity
# (app/auth/proxy/). The value was pre-authorised by v378's CHECK swap, so this phase
# needed no second constraint change on a live "user" table.
AUTH_TYPE_PROXY = "proxy"
AUTH_TYPE_SAML = "saml"

# All valid core auth types. Registry-based external providers (cloud edition)
# define their own auth-type strings in the cloud layer and register via
# app.auth.provider_registry — they are not enumerated here.
#
# This list is what the application actually supports; the DB CHECK constraint
# (v378) must always be a superset of it, which
# tests/unit/test_v378_migration_consistency.py pins.
VALID_AUTH_TYPES = [
    AUTH_TYPE_LOCAL,
    AUTH_TYPE_LDAP,
    AUTH_TYPE_OIDC,
    AUTH_TYPE_PKI,
    AUTH_TYPE_PROXY,
    AUTH_TYPE_SAML,
]

# Version of the cloud-extension seam surface (verifier registry, pipeline
# hooks, capability resolver, ExternalIdentity shape). Bump on ANY signature
# change so the private cloud repo fails loudly instead of drifting silently.
#
# v3 (0.5.x): the OIDC surface was renamed provider-neutral (v377/v378). The user
# identity columns became user.oidc_subject (the value is an OIDC `sub`, unique only
# per ISSUER — the previous name asserted a global identifier) and
# user.oidc_refresh_token, and the auth_type value became 'oidc'. The cardinality
# mapping in auth/external_sync.py and every JIT provisioning path move with them,
# so a cloud edition pinned at v2 must be updated before merge.
#
# v2 (0.5.0): vendor columns genericized (user.external_id/external_org_id,
# organization.external_org_id; billing columns removed from core
# Organization); ExternalIdentity gained email_verified (fail-closed -- JIT
# refuses email-match linking unless the IdP asserts the address verified,
# and never links super_admin accounts by email); JIT sync raises
# PermissionError -> 401 instead of surfacing 500s. The retention seam's
# candidate-window hook flipped from max to MIN override
# (set_retention_resolver(resolver, min_resolver=...)), and
# TenantUploadLimits.max_duration_seconds is now enforced at dispatch.
CLOUD_SEAM_VERSION = 3

# Auth types that support local password fallback (have local password capability)
AUTH_TYPES_SUPPORT_LOCAL_FALLBACK = [AUTH_TYPE_PKI, AUTH_TYPE_OIDC]

# Auth types that never support local password (no local password stored).
# SAML groups with LDAP here rather than OIDC/PKI's opt-in fallback: an assertion
# carries no local-credential concept, so there is nothing sensible for a per-user
# allow_local_fallback flag to opt into.
AUTH_TYPES_NO_LOCAL_FALLBACK = [AUTH_TYPE_LDAP, AUTH_TYPE_SAML]

# JWT ``type`` claim values. Every token this app mints carries one, and every
# consumer verifies the one it expects. Without that check a token minted for a
# narrow purpose is accepted wherever any token is accepted — the MFA half-token
# is handed to a client that has NOT yet passed the second factor, so treating it
# as an access token silently bypasses MFA entirely.
TOKEN_TYPE_ACCESS = "access"  # noqa: S105 # nosec B105
TOKEN_TYPE_REFRESH = "refresh"  # noqa: S105 # nosec B105
TOKEN_TYPE_MFA = "mfa"  # noqa: S105 # nosec B105

# Placeholder for external auth users who authenticate via external provider
# These users don't have local passwords. Using a distinctive value that:
# 1. Cannot be a valid bcrypt hash (starts with $2b$)
# 2. Clearly indicates intentional external authentication
# 3. Will fail any password verification attempt
EXTERNAL_AUTH_NO_PASSWORD = "!EXTERNAL_AUTH_NO_LOCAL_PASSWORD!"  # noqa: S105 # nosec B105
