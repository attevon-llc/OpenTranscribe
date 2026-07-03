"""
Authentication type constants.

Centralizes all authentication type constants to avoid magic strings
and ensure consistency across the codebase.
"""

# Authentication type constants
AUTH_TYPE_LOCAL = "local"
AUTH_TYPE_LDAP = "ldap"
AUTH_TYPE_KEYCLOAK = "keycloak"
AUTH_TYPE_PKI = "pki"

# All valid core auth types. Registry-based external providers (cloud edition)
# define their own auth-type strings in the cloud layer and register via
# app.auth.provider_registry — they are not enumerated here.
VALID_AUTH_TYPES = [
    AUTH_TYPE_LOCAL,
    AUTH_TYPE_LDAP,
    AUTH_TYPE_KEYCLOAK,
    AUTH_TYPE_PKI,
]

# Version of the cloud-extension seam surface (verifier registry, pipeline
# hooks, capability resolver, ExternalIdentity shape). Bump on ANY signature
# change so the private cloud repo fails loudly instead of drifting silently.
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
CLOUD_SEAM_VERSION = 2

# Auth types that support local password fallback (have local password capability)
AUTH_TYPES_SUPPORT_LOCAL_FALLBACK = [AUTH_TYPE_PKI, AUTH_TYPE_KEYCLOAK]

# Auth types that never support local password (no local password stored)
AUTH_TYPES_NO_LOCAL_FALLBACK = [AUTH_TYPE_LDAP]

# Placeholder for external auth users who authenticate via external provider
# These users don't have local passwords. Using a distinctive value that:
# 1. Cannot be a valid bcrypt hash (starts with $2b$)
# 2. Clearly indicates intentional external authentication
# 3. Will fail any password verification attempt
EXTERNAL_AUTH_NO_PASSWORD = "!EXTERNAL_AUTH_NO_LOCAL_PASSWORD!"  # noqa: S105 # nosec B105
