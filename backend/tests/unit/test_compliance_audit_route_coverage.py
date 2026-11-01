"""Every compliance action names the audit event it writes, and still reaches its emitter.

The register is ``fixtures/compliance_audit_routes.py``; the live proof that each event really
lands in the audit index is ``integration/test_compliance_audit_rows.py``. This half needs no
stack, so a new compliance route with no audit decision, or a deleted emitter, fails the fast
suite and CI.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fixtures.audit_reachability import reachable_events
from fixtures.audit_reachability import route_events
from fixtures.compliance_audit_routes import AUDITED_ROUTES
from fixtures.compliance_audit_routes import COMPLIANCE_EVENTS
from fixtures.compliance_audit_routes import COMPLIANCE_PATH_FAMILIES
from fixtures.compliance_audit_routes import DEPENDENCY_EMITTERS
from fixtures.compliance_audit_routes import NON_ROUTE_EVENTS
from fixtures.compliance_audit_routes import NOT_AUDITED

from app.auth.audit import AuditEventType

TESTS = Path(__file__).resolve().parents[1]


def _routes() -> dict[tuple[str, str], APIRoute]:
    from app.main import app

    table: dict[tuple[str, str], APIRoute] = {}
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in route.methods:
                table[(method, route.path)] = route
    return table


ROUTES = _routes()


def test_every_compliance_family_route_is_classified():
    in_family = {
        key
        for key in ROUTES
        if key[0] != "HEAD" and any(p.search(key[1]) for p in COMPLIANCE_PATH_FAMILIES)
    }
    assert in_family, "the family patterns match no route at all"
    unclassified = sorted(in_family - AUDITED_ROUTES.keys() - NOT_AUDITED.keys())
    assert not unclassified, (
        "compliance routes with no audit decision; add each to AUDITED_ROUTES (and a live case) "
        f"or NOT_AUDITED with a reason in fixtures/compliance_audit_routes.py: {unclassified}"
    )


def test_every_route_that_reaches_a_compliance_event_is_classified():
    reaching = {
        key
        for key, route in ROUTES.items()
        if key[0] != "HEAD" and route_events(route) & COMPLIANCE_EVENTS
    }
    assert ("POST", "/api/org-admin/holds") in reaching, "the reachability walk found nothing"
    unclassified = sorted(reaching - AUDITED_ROUTES.keys() - NOT_AUDITED.keys())
    assert not unclassified, (
        f"routes that write a compliance event but are not declared: {unclassified}"
    )


@pytest.mark.parametrize("key", sorted(AUDITED_ROUTES), ids=lambda k: f"{k[0]} {k[1]}")
def test_audited_route_still_reaches_its_event(key):
    assert key in ROUTES, f"stale register entry, no such route: {key}"
    missing = set(AUDITED_ROUTES[key].events) - route_events(ROUTES[key])
    assert not missing, f"{key} no longer reaches an emitter of {sorted(missing)}"


@pytest.mark.parametrize("dotted", sorted(DEPENDENCY_EMITTERS))
def test_dependency_still_reaches_its_event(dotted):
    module, _, name = dotted.rpartition(".")
    dependency = getattr(importlib.import_module(module), name)
    missing = set(DEPENDENCY_EMITTERS[dotted]) - reachable_events(dependency)
    assert not missing, f"{dotted} no longer reaches an emitter of {sorted(missing)}"


def test_register_entries_are_live_routes_and_name_real_evidence():
    stale = sorted(k for k in NOT_AUDITED if k not in ROUTES)
    assert not stale, f"NOT_AUDITED names routes that no longer exist: {stale}"
    for key, entry in AUDITED_ROUTES.items():
        evidence = [e for e in (entry.live, entry.pinned_by, entry.backlog) if e is not None]
        assert len(evidence) == 1, f"{key}: exactly one of live / pinned_by / backlog"
        if entry.pinned_by is not None:
            assert (TESTS / entry.pinned_by).is_file(), f"{key}: {entry.pinned_by} does not exist"
    members = set(AuditEventType.__members__)
    assert members >= COMPLIANCE_EVENTS, sorted(COMPLIANCE_EVENTS - members)


def test_every_compliance_event_is_written_somewhere_declared():
    declared = {e for entry in AUDITED_ROUTES.values() for e in entry.events}
    declared |= {e for events in DEPENDENCY_EMITTERS.values() for e in events}
    orphaned = sorted(COMPLIANCE_EVENTS - declared - NON_ROUTE_EVENTS.keys())
    assert not orphaned, f"compliance events no declared route or dependency writes: {orphaned}"


def test_the_walk_sees_an_emitter_two_calls_deep_and_ignores_signatures():
    """Guard the guard: a walk that finds nothing would pass the reachability tests vacuously."""
    from app.api.endpoints import admin

    assert "ADMIN_USER_DELETE" in reachable_events(admin.admin_erase_user)
    assert not reachable_events(lambda: None)
