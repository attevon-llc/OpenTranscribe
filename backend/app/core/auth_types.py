"""Authentication-type and token-type constants.

Lives in ``core`` because the ``user`` / ``user_invitation`` models render their CHECK
constraints from it and ``core.security`` stamps the token types; ``app.auth.constants``
keeps the auth-layer policy built on top of these.
"""

# Authentication type constants
AUTH_TYPE_LOCAL = "local"
AUTH_TYPE_LDAP = "ldap"
AUTH_TYPE_OIDC = "oidc"
AUTH_TYPE_PKI = "pki"
# Trusted-header authentication: an authenticating reverse proxy asserts the identity
# (app/auth/proxy/). The value was pre-authorised by v380's CHECK swap, so this phase
# needed no second constraint change on a live "user" table.
AUTH_TYPE_PROXY = "proxy"
AUTH_TYPE_SAML = "saml"

# All valid core auth types. Registry-based external providers (cloud edition)
# define their own auth-type strings in the cloud layer and register via
# app.auth.provider_registry — they are not enumerated here.
#
# This list is what the application actually supports; the DB CHECK constraint
# (DB_ADMITTED_AUTH_TYPES below) must always be a superset of it, which
# tests/unit/test_v380_migration_consistency.py pins.
VALID_AUTH_TYPES = [
    AUTH_TYPE_LOCAL,
    AUTH_TYPE_LDAP,
    AUTH_TYPE_OIDC,
    AUTH_TYPE_PKI,
    AUTH_TYPE_PROXY,
    AUTH_TYPE_SAML,
]

# The value set the database CHECKs (``ck_user_auth_type_valid`` on ``user`` and
# ``ck_user_invitation_auth_type_valid`` on ``user_invitation``) admit. THE single home
# of that set: the model-declared CHECK bodies are rendered from it
# (``auth_type_check_sql``), and ``tests/unit/test_auth_type_allowlist_single_source.py``
# pins the latest migration that sets the constraint (v383) to it, so the model, the
# migrations and this constant cannot drift apart silently (issue #866).
#
# It is deliberately a SEPARATE tuple from VALID_AUTH_TYPES, which it must contain: a
# value may be admitted by the database before any code supports it (a widening
# migration ships ahead of its provider). Add a value here only in the same commit as
# the migration that widens the DDL, and keep that migration ADDITIVE (see below).
#
# Registry-based providers (app.auth.provider_registry) are NOT listed: an edition that
# registers a verifier for its own ``auth_type`` string must widen both CHECKs with its
# own migration, and ``external_sync`` refuses (with a message naming the fix) a provider
# the live constraint does not admit instead of surfacing a bare CheckViolation. Core
# migrations that touch these CHECKs must never rewrite unlisted values to 'local'
# (``UPDATE ... WHERE auth_type NOT IN (...)``, the v377/v380 repair): that would demote
# an edition's delegated-auth accounts to password logins with no password. The pattern
# is closed to new migrations by the same test file.
DB_ADMITTED_AUTH_TYPES: tuple[str, ...] = (
    AUTH_TYPE_LOCAL,
    AUTH_TYPE_LDAP,
    AUTH_TYPE_OIDC,
    AUTH_TYPE_PKI,
    AUTH_TYPE_PROXY,
    AUTH_TYPE_SAML,
)


def auth_type_check_sql() -> str:
    """The body of the ``auth_type`` CHECK constraints, rendered from the single source."""
    values = ", ".join(f"'{value}'" for value in DB_ADMITTED_AUTH_TYPES)
    return f"auth_type IN ({values})"


# JWT ``type`` claim values. Every token this app mints carries one, and every
# consumer verifies the one it expects. Without that check a token minted for a
# narrow purpose is accepted wherever any token is accepted — the MFA half-token
# is handed to a client that has NOT yet passed the second factor, so treating it
# as an access token silently bypasses MFA entirely.
TOKEN_TYPE_ACCESS = "access"  # noqa: S105 - JWT type-claim label, not a credential  # nosec B105
TOKEN_TYPE_REFRESH = "refresh"  # noqa: S105 - JWT type-claim label, not a credential  # nosec B105
TOKEN_TYPE_MFA = "mfa"  # noqa: S105 - JWT type-claim label, not a credential  # nosec B105
