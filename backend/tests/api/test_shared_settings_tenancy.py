"""Shared media sources and shared organization context stay inside the owner's tenant.

A user can mark a media source (hostname + stored credentials) or their organization
context text as shared. "Shared" means shared with the owner's tenant:

* in an organization, with the other members of that organization;
* in a personal workspace, with other accounts that belong to no organization — which,
  in the community edition (no organizations at all), is every account, as before.

Covered: the listing endpoints, ``use-shared``, the MediaCMS credential lookup the
download worker performs, and the summarization task's shared-context resolution.
"""

from __future__ import annotations

import uuid as uuid_pkg
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from app.models.organization import Organization
from app.models.organization import OrganizationMembership
from app.models.prompt import UserSetting
from app.models.user import User
from app.models.user_media_source import UserMediaSource


def _mk_user(db, label: str) -> User:
    user = User(
        email=f"{label}_{uuid_pkg.uuid4().hex[:8]}@example.com",
        full_name=f"{label} user",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _mk_org(db, label: str) -> Organization:
    org = Organization(
        external_org_id=f"org_{label}_{uuid_pkg.uuid4().hex[:8]}",
        name=f"{label} Org",
        is_active=True,
    )
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def _join(db, org: Organization, user: User) -> None:
    db.add(OrganizationMembership(organization_id=org.id, user_id=user.id, role="org:member"))
    db.commit()


def _share_source(db, owner: User) -> str:
    hostname = f"media-{uuid_pkg.uuid4().hex[:8]}.example.com"
    db.add(
        UserMediaSource(
            user_id=owner.id,
            hostname=hostname,
            provider_type="mediacms",
            username="svc",
            is_active=True,
            is_shared=True,
        )
    )
    db.commit()
    return hostname


def _share_context(db, owner: User) -> str:
    text = f"context of {owner.email}"
    db.add_all(
        [
            UserSetting(user_id=owner.id, setting_key="org_context_text", setting_value=text),
            UserSetting(
                user_id=owner.id, setting_key="org_context_is_shared", setting_value="true"
            ),
        ]
    )
    db.commit()
    return text


@contextmanager
def _acting_as(user: User, org_id: int | None):
    """Run requests as ``user`` in tenant ``org_id`` (None = personal workspace)."""
    from app.api.deps_context import RequestContext
    from app.api.deps_context import get_current_context
    from app.api.endpoints.auth import get_current_active_user
    from app.main import app

    app.dependency_overrides[get_current_active_user] = lambda: user
    app.dependency_overrides[get_current_context] = lambda: RequestContext(
        user=user, org_id=org_id, org_role="org:member" if org_id is not None else None
    )
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_current_active_user, None)
        app.dependency_overrides.pop(get_current_context, None)


@pytest.fixture()
def world(db_session):
    """Org A (a1 sharer, a2), org B (b1), personal accounts p1 (sharer) and p2."""
    db = db_session
    org_a = _mk_org(db, "A")
    org_b = _mk_org(db, "B")
    a1, a2, b1 = _mk_user(db, "a1"), _mk_user(db, "a2"), _mk_user(db, "b1")
    p1, p2 = _mk_user(db, "p1"), _mk_user(db, "p2")
    _join(db, org_a, a1)
    _join(db, org_a, a2)
    _join(db, org_b, b1)
    return type(
        "World",
        (),
        {
            "db": db,
            "org_a": org_a,
            "org_b": org_b,
            "a1": a1,
            "a2": a2,
            "b1": b1,
            "p1": p1,
            "p2": p2,
            "a1_host": _share_source(db, a1),
            "p1_host": _share_source(db, p1),
            "a1_ctx": _share_context(db, a1),
            "p1_ctx": _share_context(db, p1),
        },
    )


def _shared_hosts(client) -> set[str]:
    resp = client.get("/api/user-settings/media-sources")
    assert resp.status_code == 200, resp.text
    return {s["hostname"] for s in resp.json()["shared_sources"]}


def _shared_context_owner_ids(client) -> set[str]:
    resp = client.get("/api/user-settings/organization-context/shared")
    assert resp.status_code == 200, resp.text
    return {c["user_id"] for c in resp.json()["shared_contexts"]}


# --------------------------------------------------------------------------- #
# GET /user-settings/media-sources                                             #
# --------------------------------------------------------------------------- #


def test_shared_media_source_visible_to_same_org_member(client, world):
    with _acting_as(world.a2, world.org_a.id):
        assert world.a1_host in _shared_hosts(client)


def test_shared_media_source_hidden_from_other_org(client, world):
    with _acting_as(world.b1, world.org_b.id):
        hosts = _shared_hosts(client)
    assert world.a1_host not in hosts
    assert world.p1_host not in hosts


def test_org_shared_media_source_hidden_from_personal_scope(client, world):
    with _acting_as(world.p2, None):
        hosts = _shared_hosts(client)
    assert world.a1_host not in hosts
    # Community invariance: personal accounts still share with each other.
    assert world.p1_host in hosts


def test_org_member_in_personal_scope_sees_only_personal_shares(client, world):
    with _acting_as(world.a2, None):
        hosts = _shared_hosts(client)
    assert world.a1_host not in hosts
    assert world.p1_host in hosts


# --------------------------------------------------------------------------- #
# GET /organization-context/shared + POST /organization-context/use-shared     #
# --------------------------------------------------------------------------- #


def test_shared_org_context_visible_to_same_org_member(client, world):
    with _acting_as(world.a2, world.org_a.id):
        assert str(world.a1.id) in _shared_context_owner_ids(client)


def test_shared_org_context_hidden_from_other_org(client, world):
    with _acting_as(world.b1, world.org_b.id):
        owners = _shared_context_owner_ids(client)
    assert str(world.a1.id) not in owners
    assert str(world.p1.id) not in owners


def test_shared_org_context_personal_scope_sees_only_personal(client, world):
    with _acting_as(world.p2, None):
        owners = _shared_context_owner_ids(client)
    assert str(world.a1.id) not in owners
    assert str(world.p1.id) in owners


def test_use_shared_org_context_rejected_across_orgs(client, world):
    with _acting_as(world.b1, world.org_b.id):
        resp = client.post(
            "/api/user-settings/organization-context/use-shared",
            json={"user_id": str(world.a1.id)},
        )
    assert resp.status_code == 404, resp.text
    stored = (
        world.db.query(UserSetting)
        .filter(
            UserSetting.user_id == world.b1.id,
            UserSetting.setting_key == "org_context_use_shared_from",
        )
        .first()
    )
    assert stored is None


def test_use_shared_org_context_allowed_within_org(client, world):
    with _acting_as(world.a2, world.org_a.id):
        resp = client.post(
            "/api/user-settings/organization-context/use-shared",
            json={"user_id": str(world.a1.id)},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["using_shared_from"] == str(world.a1.id)


# --------------------------------------------------------------------------- #
# Worker paths: summarization context + MediaCMS credential lookup             #
# --------------------------------------------------------------------------- #


def _point_at(db, user: User, owner: User) -> None:
    db.add(
        UserSetting(
            user_id=user.id, setting_key="org_context_use_shared_from", setting_value=str(owner.id)
        )
    )
    db.commit()


@patch("app.utils.prompt_manager.get_user_active_prompt_info", return_value=("p", True))
def test_summarization_ignores_cross_org_shared_context(_prompt, world):
    """A pre-existing pointer to another org's context must not be honoured."""
    from app.tasks.summarization import _get_organization_context

    _point_at(world.db, world.b1, world.a1)
    assert _get_organization_context(world.db, world.b1.id) != world.a1_ctx


@patch("app.utils.prompt_manager.get_user_active_prompt_info", return_value=("p", True))
def test_summarization_uses_same_org_shared_context(_prompt, world):
    from app.tasks.summarization import _get_organization_context

    _point_at(world.db, world.a2, world.a1)
    assert _get_organization_context(world.db, world.a2.id) == world.a1_ctx


def _visible_hosts(db, user_id: int | None) -> set[str]:
    from app.services.protected_media_plugins.mediacms import MediacmsProvider

    return {s.hostname for s in MediacmsProvider._query_user_media_sources(db, user_id)}


def test_mediacms_credentials_not_usable_across_orgs(world):
    hosts = _visible_hosts(world.db, world.b1.id)
    assert world.a1_host not in hosts
    assert world.p1_host not in hosts


def test_mediacms_credentials_usable_within_org(world):
    assert world.a1_host in _visible_hosts(world.db, world.a2.id)


def test_mediacms_personal_accounts_share_with_each_other(world):
    hosts = _visible_hosts(world.db, world.p2.id)
    assert world.p1_host in hosts
    assert world.a1_host not in hosts


def test_mediacms_without_user_exposes_no_per_user_sources(world):
    """The unauthenticated-context path (public auth config) must not list any tenant's hosts."""
    assert _visible_hosts(world.db, None) == set()


def test_protected_media_auth_config_is_resolved_for_the_caller(monkeypatch):
    """The config endpoint's aggregator hands the caller to providers that accept one."""
    from app.services import protected_media_providers as pmp

    seen: list[int | None] = []

    class _Provider:
        def get_public_auth_config(self, user_id: int | None = None) -> dict:
            seen.append(user_id)
            return {"hosts": ["h.example.com"]}

    monkeypatch.setattr(pmp, "PROTECTED_MEDIA_PROVIDERS", [_Provider()])
    assert pmp.get_protected_media_auth_config(user_id=42) == [{"hosts": ["h.example.com"]}]
    assert seen == [42]
