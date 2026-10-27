"""Assigning a new watch source to another account follows the tenancy mode.

``assign_to_user_uuid`` lets an admin stand up a source whose imports land in someone else's
library. In a multi-tenant deployment the target's workspace (a personal one is its own tenant)
is reachable only where the request's bypass allows a write there, and a refusal is the same 404
an unknown account gets. A single-tenant deployment keeps the instance-wide behaviour.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.config import settings
from app.models.watch_source import WatchSource
from app.services.platform_access import reset_tenancy_mode_cache

CREATE = "/api/watch-sources"


@pytest.fixture
def world(support_world, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "WATCH_FOLDER_PATH", str(tmp_path))
    return support_world


def _body(name: str, assign_to=None) -> dict:
    body = {"name": name, "source_type": "local", "local_path": "inbox"}
    if assign_to is not None:
        body["assign_to_user_uuid"] = str(assign_to)
    return body


def _rows(db, name: str) -> list[WatchSource]:
    db.expire_all()
    found: list[WatchSource] = db.query(WatchSource).filter(WatchSource.name == name).all()
    return found


def test_a_platform_admin_cannot_assign_to_an_unrelated_personal_workspace(world):
    w = world
    name = f"assign-{uuid.uuid4().hex[:8]}"
    refused = w.client.post(CREATE, headers=w.admin_headers, json=_body(name, w.owner.uuid))
    assert refused.status_code == 404, refused.text
    assert _rows(w.db, name) == []


def test_the_refusal_is_the_answer_for_an_unknown_account(world):
    w = world
    known = w.client.post(
        CREATE, headers=w.admin_headers, json=_body(f"a-{uuid.uuid4().hex[:6]}", w.owner.uuid)
    )
    unknown = w.client.post(
        CREATE, headers=w.admin_headers, json=_body(f"b-{uuid.uuid4().hex[:6]}", uuid.uuid4())
    )
    assert known.status_code == unknown.status_code == 404
    assert known.json() == unknown.json()


def test_a_platform_admin_cannot_assign_inside_an_organization_either(world):
    w = world
    w.roles[w.admin.id] = (w.org.id, "org:member")
    w.roles[w.owner.id] = (w.org.id, "org:member")
    name = f"assign-{uuid.uuid4().hex[:8]}"
    refused = w.client.post(CREATE, headers=w.admin_headers, json=_body(name, w.owner.uuid))
    assert refused.status_code == 404, refused.text
    assert _rows(w.db, name) == []


def test_an_admin_can_still_create_a_source_for_themselves(world):
    """The control: the same request without a foreign target is served."""
    w = world
    name = f"own-{uuid.uuid4().hex[:8]}"
    for suffix, target in (("plain", None), ("self", w.admin.uuid)):
        served = w.client.post(
            CREATE, headers=w.admin_headers, json=_body(f"{name}-{suffix}", target)
        )
        assert served.status_code == 200, served.text
    owners = [r.user_id for s in ("plain", "self") for r in _rows(w.db, f"{name}-{s}")]
    assert owners == [w.admin.id, w.admin.id]


def test_a_single_tenant_admin_still_assigns_to_another_user(world, monkeypatch):
    w = world
    monkeypatch.setattr(settings, "TENANCY_MODE", "single")
    reset_tenancy_mode_cache()
    try:
        name = f"assign-{uuid.uuid4().hex[:8]}"
        served = w.client.post(CREATE, headers=w.admin_headers, json=_body(name, w.owner.uuid))
    finally:
        reset_tenancy_mode_cache()
    assert served.status_code == 200, served.text
    [row] = _rows(w.db, name)
    assert row.user_id == w.owner.id
    assert row.created_by == w.admin.id


def test_a_plain_user_cannot_assign_a_source_to_anyone_else(world):
    """The assignment is an admin's tool: from anyone else the field is ignored, not honoured."""
    w = world
    name = f"ignored-{uuid.uuid4().hex[:8]}"
    served = w.client.post(CREATE, headers=w.owner_headers, json=_body(name, w.admin.uuid))
    assert served.status_code == 200, served.text
    [row] = _rows(w.db, name)
    assert row.user_id == w.owner.id, "the source belongs to the caller, not the named account"
    assert row.created_by == w.owner.id


def test_a_plain_user_in_a_single_tenant_deployment_cannot_assign_either(world, monkeypatch):
    w = world
    monkeypatch.setattr(settings, "TENANCY_MODE", "single")
    reset_tenancy_mode_cache()
    try:
        name = f"ignored-{uuid.uuid4().hex[:8]}"
        served = w.client.post(CREATE, headers=w.owner_headers, json=_body(name, w.admin.uuid))
    finally:
        reset_tenancy_mode_cache()
    assert served.status_code == 200, served.text
    [row] = _rows(w.db, name)
    assert row.user_id == w.owner.id
