"""The three-tier privilege contract, pinned against the real router table.

``User.role ∈ {user, admin, super_admin}`` is the sole authorization truth
(``app/auth/roles.py``). The dividing rule this test enforces:

    Anything that changes how the DEPLOYMENT runs, or that stores infrastructure
    credentials, is super_admin. Anything that manages users and their content
    within the deployment is admin.

Before this, six routers holding deployment configuration — including the backup
and media-mirror destinations and the watch-source connectors, all of which store
S3/SMB/SMTP credentials — were reachable by any ``admin``. And the super_admin
gate itself was declared three separate times, each comparing against its own
``"super_admin"`` string literal instead of ``roles.ROLE_SUPER_ADMIN``.

This test walks the mounted FastAPI dependency tree, so a new endpoint cannot
quietly ship at the wrong tier.
"""

from __future__ import annotations

from typing import Any

import pytest

TIER_PUBLIC = "public"
#: Authenticates optionally and then authorizes in the handler body (the
#: thumbnail endpoint serves public files anonymously but gates private ones on
#: ownership/sharing). Gated, just not by a dependency.
TIER_OPTIONAL = "optional"
TIER_USER = "user"
TIER_ADMIN = "admin"
TIER_SUPER_ADMIN = "super_admin"

#: Path prefixes that must be super_admin because they configure the deployment
#: or hold infrastructure credentials.
SUPER_ADMIN_PREFIXES = (
    "/api/admin/auth-config",
    "/api/admin/engine-settings",
    "/api/admin/backup",
    "/api/admin/media-mirror",
    "/api/admin/redaction-policy",
    # A group mapping decides who a directory claim hands admin to — that is
    # authorization configuration, not team management.
    "/api/admin/group-mappings",
    # A SCIM token can create and disable accounts across the whole deployment. That
    # is an infrastructure credential, in the same class as the LDAP bind password.
    "/api/admin/scim-tokens",
    "/api/watch-sources/settings",
    "/api/watch-sources/email-configs",
)

#: Endpoints that are deliberately unauthenticated, each with a reviewed reason.
#: Anything NOT listed here and not otherwise gated is a finding, not an omission.
KNOWN_PUBLIC = {
    # Pre-authentication by definition — these are how you get a session.
    "/api/auth/token",
    "/api/auth/login",
    "/api/auth/register",
    "/api/auth/token/refresh",
    "/api/auth/mfa/verify",
    "/api/auth/oidc/login",
    "/api/auth/oidc/callback",
    "/api/auth/pki/authenticate",
    # SAML SP endpoints. /metadata is fetched out-of-band by an IdP administrator
    # (no secret in it — see saml.py's docstring); /login, /acs and /sls are the
    # pre-authentication SSO round trip and the IdP's own POST/redirect targets,
    # so a session dependency would make them unreachable, the same reasoning as
    # the OIDC pair above.
    "/api/auth/saml/metadata",
    "/api/auth/saml/login",
    "/api/auth/saml/acs",
    "/api/auth/saml/sls",
    # Trusted-header sign-in. Pre-authentication by definition, and the credential
    # is the header an allowlisted reverse proxy put on the request — a session
    # dependency here would make it unreachable. The trust check is
    # auth/header_trust.py, which refuses every assertion when no allowlist is set.
    "/api/auth/proxy/authenticate",
    "/api/auth/password-reset/request",
    "/api/auth/password-reset/confirm",
    # Redeeming an admin invitation IS the pre-session step that creates the
    # account; the bearer of the single-use token is the only credential there
    # is. Rate-limited, and every bad token gets one identical error.
    "/api/auth/invitations/lookup",
    "/api/auth/invitations/accept",
    # An account blocked BY require_email_verification cannot authenticate to
    # verify itself; the emailed token is the credential. Resend is deliberately
    # answer-identical so it is not an account-existence oracle.
    "/api/auth/verify-email",
    "/api/auth/verify-email/resend",
    # Read by the login page before a session exists.
    "/api/auth/methods",
    "/api/auth/banner",
    "/api/auth/password-policy",
    # The SPA's session probe: 200 for anonymous by design, never 401.
    "/api/auth/session",
    # Ends a session; must succeed even with an expired or absent token.
    "/api/auth/logout",
    # Fetched before initAuth() on first paint to decide which UI to render.
    "/api/system/capabilities",
    # Static catalogues with no deployment or user data in them.
    "/api/files/supported-formats",
    "/api/llm-settings/providers",
    # Infrastructure probes, network-gated rather than dependency-gated.
    "/health",
    "/health/ready",
    "/metrics",
}


def _tier_of(route) -> str:
    """Classify a route by the strongest role dependency in its dependency tree."""
    from app.api.endpoints.auth.dependencies import get_current_active_superuser
    from app.api.endpoints.auth.dependencies import get_current_active_user
    from app.api.endpoints.auth.dependencies import get_current_admin_user
    from app.api.endpoints.auth.dependencies import get_current_user
    from app.api.endpoints.auth.dependencies import get_optional_current_user

    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return TIER_PUBLIC

    seen: set[Any] = set()
    stack = [dependant]
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if node.call is not None:
            seen.add(node.call)
        stack.extend(node.dependencies)

    if get_current_active_superuser in seen:
        return TIER_SUPER_ADMIN
    if get_current_admin_user in seen:
        return TIER_ADMIN
    if get_current_active_user in seen or get_current_user in seen:
        return TIER_USER
    # Forced MFA enrolment: accepts a normal session OR an enrolment-scoped
    # half-token, so it authenticates — just not via the standard dependency.
    from app.api.endpoints.auth.mfa_enrollment import get_user_for_enrollment

    if get_user_for_enrollment in seen:
        return TIER_USER
    if get_optional_current_user in seen:
        return TIER_OPTIONAL
    return TIER_PUBLIC


@pytest.fixture(scope="module")
def routes():
    from app.main import app

    return [r for r in app.routes if hasattr(r, "path") and hasattr(r, "methods")]


class TestSingleDefinitionOfTheSuperAdminGate:
    def test_admin_module_reexports_the_canonical_dependency(self):
        from app.api.endpoints import admin
        from app.api.endpoints.auth.dependencies import get_current_active_superuser

        assert admin.get_current_super_admin_user is get_current_active_superuser

    def test_auth_config_module_reexports_the_canonical_dependency(self):
        from app.api.endpoints import auth_config
        from app.api.endpoints.auth.dependencies import get_current_active_superuser

        assert auth_config.get_current_super_admin_user is get_current_active_superuser

    def test_canonical_gate_uses_the_role_constant_not_a_literal(self):
        import inspect

        from app.api.endpoints.auth.dependencies import get_current_active_superuser

        source = inspect.getsource(get_current_active_superuser)
        assert "ROLE_SUPER_ADMIN" in source
        assert '"super_admin"' not in source


class TestDeploymentConfigurationIsSuperAdmin:
    def test_every_deployment_config_route_requires_super_admin(self, routes):
        offenders = [
            f"{sorted(r.methods)} {r.path} -> {_tier_of(r)}"
            for r in routes
            if r.path.startswith(SUPER_ADMIN_PREFIXES) and _tier_of(r) != TIER_SUPER_ADMIN
        ]
        assert not offenders, (
            "these routes configure the deployment or hold infrastructure "
            f"credentials and must be super_admin: {offenders}"
        )


class TestNoAccidentallyPublicRoutes:
    def test_every_route_is_gated_or_explicitly_public(self, routes):
        ungated = sorted(
            {
                r.path
                for r in routes
                if _tier_of(r) == TIER_PUBLIC
                and r.path not in KNOWN_PUBLIC
                and r.path.startswith("/api/")
                # Docs/OpenAPI are gated in main.py on is_hardened, not by a dependency.
                and not r.path.startswith(("/api/docs", "/api/redoc", "/api/openapi"))
            }
        )
        assert not ungated, (
            "unauthenticated routes that are not on the reviewed public list: "
            f"{ungated} — add a dependency, or add to KNOWN_PUBLIC with justification"
        )


class TestSCIMIsBearerTokenGated:
    """``/scim/v2`` sits outside ``/api``, so the sweep above never sees it.

    Its credential is a hashed, revocable bearer token rather than a session, so it
    is invisible to ``_tier_of`` — which is exactly why it needs its own assertion
    rather than an entry on ``KNOWN_PUBLIC``.
    """

    def test_every_scim_route_requires_the_scim_token_dependency(self, routes):
        from app.api.endpoints.scim.auth import require_scim_token

        scim_routes = [r for r in routes if r.path.startswith("/scim/")]
        assert scim_routes, "no SCIM routes are mounted — the check below would be vacuous"

        ungated = []
        for route in scim_routes:
            dependant = getattr(route, "dependant", None)
            calls = set()
            stack = [dependant] if dependant else []
            while stack:
                node = stack.pop()
                if node.call is not None:
                    calls.add(node.call)
                stack.extend(node.dependencies)
            if require_scim_token not in calls:
                ungated.append(f"{sorted(route.methods)} {route.path}")
        assert not ungated, f"SCIM routes without bearer-token authentication: {ungated}"

    def test_scim_token_management_is_super_admin(self, routes):
        token_routes = [r for r in routes if r.path.startswith("/api/admin/scim-tokens")]
        assert token_routes
        assert all(_tier_of(r) == TIER_SUPER_ADMIN for r in token_routes)
