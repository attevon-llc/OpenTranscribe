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
# External managed-IdP provider used by the commercial cloud edition. Core only
# defines the constant + seams; the verifier implementation lives in the
# private cloud layer and registers itself via app.auth.provider_registry.
AUTH_TYPE_CLERK = "clerk"

# All valid auth types
VALID_AUTH_TYPES = [
    AUTH_TYPE_LOCAL,
    AUTH_TYPE_LDAP,
    AUTH_TYPE_KEYCLOAK,
    AUTH_TYPE_PKI,
    AUTH_TYPE_CLERK,
]

# Version of the cloud-extension seam surface (verifier registry, pipeline
# hooks, capability resolver, ExternalIdentity shape). Bump on ANY signature
# change so the private cloud repo fails loudly instead of drifting silently.
CLOUD_SEAM_VERSION = 1

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
