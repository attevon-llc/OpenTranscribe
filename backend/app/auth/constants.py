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


# Version of the cloud-extension seam surface (verifier registry, pipeline
# hooks, capability resolver, ExternalIdentity shape). Bump on ANY signature
# change so the private cloud repo fails loudly instead of drifting silently.
#
# v12 (0.6.0): organization content governance (#1275-#1277). v11 is the privacy policy layer and
# v10 the viewer-aware capabilities endpoint, both below.
# File-read semantics changed: a member of an organization collection they may see now
# reads the organization's files in it on EVERY surface (file page, gallery, search, chat
# retrieval); before, only the collection's own file list honoured it. The rule is
# ``services.collection_visibility`` (one predicate) and the search clause is
# ``services.search.access_filter.access_filter_clause``. A deployment that registers hooks
# reading collection or file visibility must call those rather than re-derive it. Index
# documents are unchanged (``collection_ids`` already existed) and no reindex is needed.
# New capability keys that a resolver must return (omitted keys fail closed, v6) are added
# alongside the audit work: ``audit.content_access``, ``org.audit_logs``, ``org.legal_hold``.
#
# v11: privacy policy layer (issues #1280, #1279). Additive. ``app.services.privacy.consent.
# register_voiceprint_consent_hook(hook)``: a ``(Session, VoiceprintConsentRequest) -> bool``
# hook must return the literal True for a voiceprint to be created (``purpose="create"``) or
# matched across owners (``purpose="share"``); a hook that raises or returns anything else
# denies, and with no hook registered nothing changes. ``app.services.privacy.policy.
# set_privacy_ceiling_resolver(resolver)``: a ``(Session, organization_id | None) ->
# PrivacyCeiling | None`` resolver is the platform layer of the restrict-only voiceprint and
# gender policy; it can only switch features OFF, a raising or malformed answer denies both,
# and it runs inside Celery tasks with no request, so it must answer per organization. Alembic
# ``v445`` adds ``organization_policy`` (empty until an organization saves a choice; the
# organization redaction floor stored there is unioned with ``resolve_redaction_floor``'s
# resolver result, strictest wins, once the floor storage lands).
#
# v10: viewer-aware ``GET /system/capabilities`` (issue #1274). The endpoint now resolves
# the caller optionally and returns ``get_capabilities_for_viewer``: for platform staff
# (an active ``is_superuser``), every platform-audience key outside
# ``app.core.capabilities.NO_PLATFORM_BYPASS`` reads True even when the resolver denied it,
# omitted it, or raised, mirroring what ``require_capability``'s bypass already admits on the
# server. Anonymous and tenant callers get the resolver's answer unchanged. A resolver may
# now find ``request.state.user_id`` / ``external_identity`` at that endpoint (external
# tokens only; additive) and must still handle an anonymous request and ``request=None``.
# The response is ``Cache-Control: private, no-store`` with ``Vary: Cookie, Authorization``:
# remove any edge-cache rule that cached it. ``require_capability(..., platform_admin_bypass=False)``
# now raises ValueError at import unless the key is declared in ``NO_PLATFORM_BYPASS``.
# ``get_optional_current_user`` now honours the per-user revocation epoch (``iat``), so a
# token revoked by logout-all or a password reset no longer resolves on optional-auth routes.
# The response shape is unchanged.
#
# v9: legal document provider seam. ``app.legal.registry.set_document_provider`` lets an
# extension replace the document set served by ``/api/legal/*``. Additive: with no provider
# installed the vendored OSS set is served and the operator acknowledgment is required;
# installing any provider turns ``requires_acknowledgment`` off (the extension runs its own
# acceptance flow). No existing seam changes.
#
# v8: platform-admin content access (issue #1122). In a multi-tenant deployment
# (any active organization, or ``TENANCY_MODE=multi``) ``role in {admin, super_admin}``
# grants NO implicit access to tenant content any more: it needs a support-access grant
# (``/api/support-access``) carried in the ``X-Support-Access-Grant`` header. Staff
# tooling in the managed edition that read or edited a tenant's files through the
# platform role must request a grant first. ``RequestContext`` gained ``bypass``, and
# ``get_current_context`` was split from ``get_base_context`` (lifecycle routes never
# resolve the header). BREAKING for a cloud layer that relied on the admin bypass.
#
# v7: external-IdP session timeouts (issue #1106). Additive.
#   Backend: ExternalIdentity gained ``auth_time: int | None`` (epoch seconds the
#   person authenticated at the provider; NOT ``iat``). When set (or present as
#   ``raw_claims["auth_time"]``) and EXTERNAL_SESSION_ABSOLUTE_TIMEOUT_ENFORCED is on
#   (default), a token whose auth_time is older than SESSION_ABSOLUTE_TIMEOUT_MINUTES
#   is refused: 401 ``detail.code == "session_expired"`` on required-auth routes,
#   anonymous on optional-auth routes, socket refused. A verifier that supplies
#   neither is NOT enforced (one warning per provider) -- a v6 verifier keeps working
#   unchanged, but gets no server-side absolute timeout until it populates auth_time.
#   ``GET /auth/methods`` gained ``session_idle_timeout_minutes`` /
#   ``session_absolute_timeout_minutes``.
#   Frontend ``$lib/cloud`` seam gained two OPTIONAL exports, both resolved at
#   runtime so an overlay that lacks them still builds and runs:
#   ``getExternalSessionAuthTime(): Promise<number | null>`` (epoch seconds; the
#   idle guard falls back to first-seen when null) and
#   ``externalReauthenticate(reason): Promise<boolean>`` (true = the overlay started
#   its own re-authentication; false = core signs out via ``externalSignOut``).
#
# v6: capability resolver results fail CLOSED (issue #868). core.capabilities.
# get_capabilities no longer merges a resolver's result over COMMUNITY_CAPABILITIES:
# a known key the resolver omits, a non-bool value, a None/non-dict result, or a
# resolver that raises now reads as DENIED instead of inheriting the community
# default (True for almost every key) or propagating the exception. BREAKING for a
# resolver that relied on omission to mean "community default" — it must return
# every key it grants (start from {**COMMUNITY_CAPABILITIES, ...} to keep that
# behaviour deliberately). The community resolver is unaffected.
#
# v5: user.platform_super_admin_link_authorized (issue #993). Additive escape hatch
# for auth.account_linking.assert_provider_id_link_permitted's rule 1, which
# otherwise unconditionally refuses to JIT-link/refresh an external identity onto a
# role == super_admin row. Defaults False and nothing in core ever sets it; a v4
# cloud layer keeps working exactly as before (still unconditionally refused) until
# it deliberately opts a specific row in via its own out-of-band admin-grant
# mechanism. Rule 2 (email corroboration) is untouched and still runs
# unconditionally, flag or not.
#
# v4 (0.6.0): the before-dispatch pipeline hook gained an explicit fail-closed
# signal. ``tasks.transcription.hooks.DispatchBlockedError`` propagates out of
# fire_before_dispatch alongside QuotaExceededError; every OTHER hook exception is
# still contained. Additive — a v3 cloud layer keeps working — but a hook that
# raised its own exception type meaning to block was being silently ignored and
# the job dispatched anyway, so a cloud edition pinned at v3 should audit its
# before-dispatch hooks for exactly that.
#   Also v4: ``services.usage_service.record_event`` no longer swallows write
#   failures. True/False now mean "recorded"/"already recorded by an earlier
#   attempt" and nothing else; a real DB failure raises. THIS ONE IS BREAKING for
#   any cloud caller that treated False as "contained, carry on" — it will now see
#   the exception and must decide to retry, alarm, or contain it itself.
#   Also v4: ``core.tenant_limits`` gained a third registry hook,
#   set_redaction_floor_resolver / resolve_redaction_floor(db, org_id) -> RedactionFloor.
#   Additive; its community resolver returns None, and the floor it returns can only
#   ADD to the global redaction.force_* settings, never relax them.
#
# v3 (0.5.x): the OIDC surface was renamed provider-neutral (v379/v380). The user
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
CLOUD_SEAM_VERSION = 12

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
