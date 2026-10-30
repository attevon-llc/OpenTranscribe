"""Every per-source watch-source route goes through the tenant gate.

``_get_source_or_404`` is the one chokepoint for a ``/{source_uuid}`` route: another tenant's
source answers the same 404 as a missing one, and another user's source in the caller's own
tenant answers 403. ``test_dedup_sharing_tenancy.py`` pins that for the detail and file-history
reads only; this sweeps every route, so a handler that loads the row itself (or a new route that
forgets the gate) fails here. The route list is checked against the live route table, so a new
``/{source_uuid}`` route cannot be added without a row below.

Each refusal is paired with a positive control on the same route: the owner in the source's
tenant gets the route's own answer (``OWNER_OUTCOME``), so the refusals are the gate firing
rather than a request body the route would have rejected anyway.
"""

from __future__ import annotations

import uuid as uuid_pkg
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fixtures.tenant_rows import make_watch_source

from app.models.organization import Organization
from app.models.user import User

PREFIX = "/api/watch-sources/{source_uuid}"
GATE_404 = "Watch source not found"
GATE_403 = "Not authorized for this watch source"

_ANY_UUID = str(uuid_pkg.uuid4())
_FILE_ACTION = {"file_uuids": [_ANY_UUID]}

#: (method, path template under the source, JSON body or None). Child uuids are random: the gate
#: runs before the child is looked up, and the positive control accepts the child's own 404.
SOURCE_ROUTES: list[tuple[str, str, dict | None]] = [
    ("GET", "", None),
    ("PUT", "", {}),
    ("DELETE", "", None),
    ("POST", "/test", None),
    ("POST", "/scan", None),
    ("GET", "/files", None),
    ("GET", "/files/stats", None),
    ("DELETE", "/files/{file_uuid}", None),
    ("POST", "/files/retry", _FILE_ACTION),
    ("POST", "/files/bulk-delete", _FILE_ACTION),
    ("GET", "/emails", None),
    ("GET", "/emails/available", None),
    ("POST", "/emails", {"email_config_uuid": _ANY_UUID}),
    ("DELETE", "/emails/{config_uuid}", None),
]
_IDS = [f"{method} {path or '/'}" for method, path, _ in SOURCE_ROUTES]

#: What the owner gets past the gate: (status, detail or None). The three 404s are the random
#: child uuid not being found, which only happens once the source itself has resolved.
OWNER_OUTCOME: dict[tuple[str, str], tuple[int, str | None]] = {
    ("GET", ""): (200, None),
    ("PUT", ""): (200, None),
    ("DELETE", ""): (200, None),
    ("POST", "/test"): (200, None),
    ("POST", "/scan"): (200, None),
    ("GET", "/files"): (200, None),
    ("GET", "/files/stats"): (200, None),
    ("DELETE", "/files/{file_uuid}"): (404, "Tracked file not found"),
    ("POST", "/files/retry"): (200, None),
    ("POST", "/files/bulk-delete"): (200, None),
    ("GET", "/emails"): (200, None),
    ("GET", "/emails/available"): (200, None),
    ("POST", "/emails"): (404, "Email config not found"),
    ("DELETE", "/emails/{config_uuid}"): (404, "Email config not found"),
}


@contextmanager
def _acting_as(user: User, org_id: int | None) -> Iterator[None]:
    from app.api.deps_context import get_current_context
    from app.api.endpoints.auth import get_current_active_user
    from app.main import app
    from app.services.request_context import RequestContext

    app.dependency_overrides[get_current_active_user] = lambda: user
    app.dependency_overrides[get_current_context] = lambda: RequestContext(
        user=user, org_id=org_id, org_role="org:member" if org_id is not None else None
    )
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_current_active_user, None)
        app.dependency_overrides.pop(get_current_context, None)


def _mk_org(db) -> Organization:
    org = Organization(
        external_org_id=f"org_gate_{uuid_pkg.uuid4().hex[:8]}", name="Gate Org", is_active=True
    )
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def _call(client, method: str, path: str, body: dict | None, source_uuid: str):
    url = (PREFIX + path).format(
        source_uuid=source_uuid, file_uuid=_ANY_UUID, config_uuid=_ANY_UUID
    )
    dispatched = SimpleNamespace(id="gate-test-task")
    with patch("app.tasks.watch_source_tasks.scan_single.delay", return_value=dispatched):
        return client.request(method, url, json=body)


def test_route_list_matches_the_live_route_table():
    from fastapi.routing import APIRoute

    from app.main import app

    live = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith(PREFIX)
        for method in route.methods
    }
    listed = {(method, PREFIX + path) for method, path, _ in SOURCE_ROUTES}
    assert listed == live
    assert set(OWNER_OUTCOME) == {(method, path) for method, path, _ in SOURCE_ROUTES}


@pytest.mark.parametrize(("method", "path", "body"), SOURCE_ROUTES, ids=_IDS)
def test_another_tenants_source_is_not_found(client, db_session, normal_user, method, path, body):
    org_a, org_b = _mk_org(db_session), _mk_org(db_session)
    source = make_watch_source(db_session, user_id=normal_user.id, org_id=org_a.id)
    with _acting_as(normal_user, org_b.id):
        response = _call(client, method, path, body, str(source.uuid))
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == GATE_404


@pytest.mark.parametrize(("method", "path", "body"), SOURCE_ROUTES, ids=_IDS)
def test_a_personal_source_is_not_found_from_an_org(
    client, db_session, normal_user, method, path, body
):
    org = _mk_org(db_session)
    source = make_watch_source(db_session, user_id=normal_user.id, org_id=None)
    with _acting_as(normal_user, org.id):
        response = _call(client, method, path, body, str(source.uuid))
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == GATE_404


@pytest.mark.parametrize(("method", "path", "body"), SOURCE_ROUTES, ids=_IDS)
def test_another_users_source_in_the_same_tenant_is_forbidden(
    client, db_session, normal_user, other_user, method, path, body
):
    org = _mk_org(db_session)
    source = make_watch_source(db_session, user_id=normal_user.id, org_id=org.id)
    with _acting_as(other_user, org.id):
        response = _call(client, method, path, body, str(source.uuid))
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == GATE_403


@pytest.mark.parametrize(("method", "path", "body"), SOURCE_ROUTES, ids=_IDS)
def test_the_owner_in_the_sources_tenant_gets_past_the_gate(
    client, db_session, normal_user, method, path, body
):
    org = _mk_org(db_session)
    source = make_watch_source(db_session, user_id=normal_user.id, org_id=org.id)
    with _acting_as(normal_user, org.id):
        response = _call(client, method, path, body, str(source.uuid))
    expected_status, expected_detail = OWNER_OUTCOME[(method, path)]
    assert response.status_code == expected_status, response.text
    if expected_detail is not None:
        assert response.json()["detail"] == expected_detail
