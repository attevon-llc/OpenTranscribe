"""Fixtures for the HTTP-level API suites under ``tests/api/``.

Two of them exist to make the **cloud tenancy seam reachable over HTTP** without a
cloud IdP. The org-scoped surfaces (org-admin, and the tenant gate on
``GET /users/search``) were previously only ever tested by calling the handler
function directly with a hand-built ``RequestContext`` — which proves the body's
logic and nothing about the dependency chain in front of it: not the capability
gate, not ``require_org_admin``, not the account-lifecycle gate, and not the
ordering between them.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest


@pytest.fixture
def org_context(monkeypatch) -> Callable[..., None]:
    """Install a fake org resolution for the duration of one test.

    ``resolve_org_context`` is the ONLY step of ``get_current_context`` that needs
    a cloud identity provider (it reads ``request.state.external_identity``, which
    the community edition never sets). Replacing just that function keeps the whole
    real chain in front of it — bearer token, ``get_current_active_user``'s
    lifecycle gates, ``require_org_admin``, the router's capability dependency — so
    a test can exercise an org-admin request end to end over HTTP.

    Overriding ``get_current_context`` itself would have been shorter and wrong:
    it would remove the authentication and lifecycle gates from the very routes
    whose gating is under test.

    Usage::

        org_context(org_id=org.id, org_role="org:admin", only_for=admin_a.id)

    Args:
        Returned callable takes ``org_id``, ``org_role`` and an optional
        ``only_for`` user id. Any other user resolves to personal scope, so one
        test can drive both an org admin and an outsider.
    """

    def _install(org_id: int | None, org_role: str | None, only_for: int | None = None) -> None:
        def _fake_resolve(_request, _db, user):
            if only_for is not None and int(user.id) != int(only_for):
                return None, None
            return org_id, org_role

        monkeypatch.setattr("app.api.deps_context.resolve_org_context", _fake_resolve)

    return _install


@pytest.fixture
def organizations_capability_on():
    """Enable the cloud-only ``organizations`` capability for one test.

    Without this the ``/api/org-admin`` router answers 404 for everyone
    (``require_capability``), which is itself asserted by
    ``test_org_admin_endpoints.py`` — so every test that wants to reach a handler
    under that prefix has to turn the surface on first, exactly as the cloud
    resolver does.
    """
    from app.core.capabilities import reset_capability_resolver
    from app.core.capabilities import set_capability_resolver

    set_capability_resolver(lambda _request: {"organizations": True})
    yield
    reset_capability_resolver()
