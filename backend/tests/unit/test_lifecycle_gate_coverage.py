"""Every authenticated route must pass through the account-lifecycle gate.

``get_current_user`` answers "is this credential valid?". ``get_current_active_user``
answers "may this account act right now?" — deactivated, unapproved, rejected,
expired, must-change-password, or banner-unacknowledged. The split is deliberate
(the credential layer is shared with the WebSocket handshake and the anonymous
session probe, neither of which wants a 403), which means a route that depends on
``get_current_user`` **directly** silently opts out of every lifecycle control.

``backend/app/api/endpoints/files/management.py`` did exactly that for all 8 of its
handlers, including ``DELETE /{file_uuid}/force`` and ``POST /management/bulk-action``
— so an expired or force-password-change admin could still force-delete files
(issue #431). ``tests/unit/test_account_lifecycle.py`` covers the dependency's own
logic; nothing checked which routes actually reach it.

This walks each route's real dependency tree rather than matching decorators, so a
route that gets there through ``get_current_admin_user`` (or any other wrapper that
chains through the gate) passes without needing to be listed here.
"""

from __future__ import annotations

from typing import Any

import pytest

#: Routes allowed to depend on the credential layer directly, with the reason.
#: Keyed by "METHOD path" so widening a route's methods does not inherit the waiver.
#:
#: Deliberately short, and shorter than it first looks. Most "remedy for its own
#: gate" routes need no waiver at all, because the exemption lives *inside*
#: ``get_current_active_user`` as a route-template check
#: (``PASSWORD_CHANGE_EXEMPT_PATHS`` / ``BANNER_EXEMPT_PATHS``): ``PUT /api/users/me``
#: and ``POST /api/auth/banner/acknowledge`` both depend on the gate and are let
#: through by it. ``GET /api/auth/session`` and ``POST /api/auth/logout`` need no
#: waiver either — they call ``get_current_user`` in their own bodies rather than as
#: a dependency, so the introspection below never sees them.
LIFECYCLE_GATE_WAIVERS: dict[str, str] = {
    # How the SPA discovers *why* it is blocked. `login()` in stores/auth.ts fetches
    # this immediately after a successful sign-in and reads `must_change_password`
    # off the payload to render the forced-change screen; gating it would 403 that
    # probe, so the SPA would enter the app shell and fail its first real request
    # instead. Exposes only the caller's own profile.
    "GET /api/auth/me": "the SPA's 'why am I blocked?' probe; renders the forced-change screen",
    # Only ever REDUCES the caller's own access. The unconditional gates (expiry,
    # rejection, pending approval) have no exempt-path escape, so gating this would
    # leave a rejected or expired account's refresh tokens rotating with no way for
    # the user to revoke them. dependencies.py lists the path in both exempt sets.
    "POST /api/auth/logout/all": "self-revocation must succeed from any account state",
    # Enforces the gate, just not through the dependency tree: nginx's auth_request
    # forwards a 403 verbatim and treats only 401 as "not authenticated", so this
    # route calls get_current_active_user + get_current_admin_user in its body and
    # normalizes every denial to 401. See app/api/endpoints/auth/flower.py.
    "GET /api/auth/flower-authz": "nginx auth_request probe; calls the gate in-body to emit 401",
}


def _dependency_callables(dependant: Any, seen: set[int] | None = None) -> set[str]:
    """Every callable name in a route's dependency tree, recursively."""
    if seen is None:
        seen = set()
    if id(dependant) in seen:
        return set()
    seen.add(id(dependant))

    names: set[str] = set()
    call = getattr(dependant, "call", None)
    if call is not None:
        names.add(getattr(call, "__name__", ""))
    for sub in getattr(dependant, "dependencies", []) or []:
        names |= _dependency_callables(sub, seen)
    return names


def _authenticated_routes() -> list[tuple[str, set[str]]]:
    """(label, dependency names) for every route that touches the credential layer."""
    from app.main import app

    out: list[tuple[str, set[str]]] = []
    for route in app.routes:
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        names = _dependency_callables(dependant)
        if "get_current_user" not in names:
            continue
        for method in sorted(getattr(route, "methods", None) or {"GET"}):
            if method in {"HEAD", "OPTIONS"}:
                continue
            out.append((f"{method} {route.path}", names))
    return out


def test_there_are_authenticated_routes_to_check():
    """Guard the guard: an introspection change that finds nothing must not pass."""
    routes = _authenticated_routes()
    assert len(routes) > 100, (
        f"only {len(routes)} authenticated routes found — dependency introspection "
        "is probably broken, which would make this whole module vacuous"
    )


def test_every_authenticated_route_reaches_the_lifecycle_gate():
    """A route on the credential layer must also reach ``get_current_active_user``."""
    offenders = [
        label
        for label, names in _authenticated_routes()
        if "get_current_active_user" not in names and label not in LIFECYCLE_GATE_WAIVERS
    ]

    assert not offenders, (
        "These routes depend on get_current_user without reaching the account-lifecycle "
        "gate, so deactivated / expired / must-change-password accounts can still call "
        "them. Depend on get_current_active_user (or a wrapper that chains through it), "
        "or add an entry to LIFECYCLE_GATE_WAIVERS with a reason:\n  "
        + "\n  ".join(sorted(offenders))
    )


@pytest.mark.parametrize("label", sorted(LIFECYCLE_GATE_WAIVERS))
def test_each_waiver_still_applies_to_a_real_route(label):
    """A waiver for a route that no longer exists is stale and must be deleted."""
    known = {route_label for route_label, _ in _authenticated_routes()}
    assert label in known, (
        f"waiver {label!r} matches no authenticated route — delete it from "
        "LIFECYCLE_GATE_WAIVERS so the list cannot rot into a blanket exemption"
    )
