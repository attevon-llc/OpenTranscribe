"""Every tag the tag API serves says whether the caller may change it.

``can_write`` must be the rule the write routes enforce, not a guess from
``ownership``: an org admin may rename a colleague's org tag that reads as
``shared_with_me``, and a plain admin of a multi-tenant deployment may not touch the
system vocabulary that every admin used to be offered. Each test pairs the reported
right with the write it predicts, so the two cannot drift apart.
"""

from __future__ import annotations

import uuid

import pytest
from fixtures.support_world import make_user
from fixtures.tag_world import attach
from fixtures.tenant_rows import make_tag

from app.core.config import settings
from app.models.sharing import TagShare
from app.models.tag import Tag
from app.services.platform_access import reset_tenancy_mode_cache


@pytest.fixture
def single_tenant(support_world, monkeypatch):
    monkeypatch.setattr(settings, "TENANCY_MODE", "single")
    reset_tenancy_mode_cache()
    yield support_world
    reset_tenancy_mode_cache()


@pytest.fixture
def owner_in_org(support_world):
    """The world's plain user acts as an ordinary member of ``org``."""
    support_world.roles[support_world.owner.id] = (support_world.org.id, "org:member")
    return support_world


def _word() -> str:
    return f"canwrite{uuid.uuid4().hex[:8]}"


def _listed(w, headers, tag: Tag) -> dict:
    response = w.client.get("/api/tags", headers=headers)
    assert response.status_code == 200, response.text
    by_uuid = {row["uuid"]: row for row in response.json()}
    assert str(tag.uuid) in by_uuid, "the tag must be listed for its write right to mean anything"
    row: dict = by_uuid[str(tag.uuid)]
    return row


def _rename(w, headers, tag: Tag) -> int:
    response = w.client.patch(
        f"/api/tags/{tag.uuid}", headers=headers, json={"name": f"{tag.name}x"}
    )
    status: int = response.status_code
    return status


def test_an_org_admin_may_write_a_colleagues_org_tag_and_is_told_so(owner_in_org):
    w = owner_in_org
    colleague = make_tag(w.db, name=_word(), user_id=w.owner.id, org_id=w.org.id)

    row = _listed(w, w.org_admin_headers, colleague)
    assert row["ownership"] == "shared_with_me"
    assert row["can_write"] is True
    assert _rename(w, w.org_admin_headers, colleague) == 200


def test_a_plain_member_is_told_a_colleagues_org_tag_is_read_only(owner_in_org):
    w = owner_in_org
    admins = make_tag(w.db, name=_word(), user_id=w.org_admin.id, org_id=w.org.id)
    own = make_tag(w.db, name=_word(), user_id=w.owner.id, org_id=w.org.id)

    row = _listed(w, w.owner_headers, admins)
    assert row["ownership"] == "shared_with_me"
    assert row["can_write"] is False
    assert _rename(w, w.owner_headers, admins) == 404

    assert _listed(w, w.owner_headers, own)["can_write"] is True
    assert _rename(w, w.owner_headers, own) == 200


def test_a_system_tag_is_writable_only_by_a_super_admin_in_a_multi_tenant_deployment(
    support_world,
):
    w = support_world
    shared = make_tag(w.db, name=_word(), user_id=None, org_id=None)

    for headers in (w.owner_headers, w.admin_headers):
        row = _listed(w, headers, shared)
        assert row["ownership"] == "system"
        assert row["can_write"] is False
        assert _rename(w, headers, shared) == 404

    assert _listed(w, w.super_headers, shared)["can_write"] is True
    assert _rename(w, w.super_headers, shared) == 200


def test_a_plain_admin_may_write_system_tags_in_a_single_tenant_deployment(single_tenant):
    w = single_tenant
    shared = make_tag(w.db, name=_word(), user_id=None, org_id=None)

    assert _listed(w, w.owner_headers, shared)["can_write"] is False
    assert _rename(w, w.owner_headers, shared) == 404
    assert _listed(w, w.admin_headers, shared)["can_write"] is True
    assert _rename(w, w.admin_headers, shared) == 200


def test_a_tag_shared_with_me_is_read_only(support_world):
    w = support_world
    sharer = make_user(w.db, role="user", label="sharer")
    granted = make_tag(w.db, name=_word(), user_id=sharer.id, org_id=None)
    w.db.add(
        TagShare(
            tag_id=granted.id,
            shared_by_id=sharer.id,
            target_type="user",
            target_user_id=w.owner.id,
        )
    )
    w.db.commit()

    row = _listed(w, w.owner_headers, granted)
    assert row["ownership"] == "shared_with_me"
    assert row["can_write"] is False
    assert _rename(w, w.owner_headers, granted) == 404


def test_collision_clusters_and_selections_carry_the_same_write_right(owner_in_org):
    w = owner_in_org
    stem = _word()
    colleague = make_tag(w.db, name=f"{stem}-alpha", user_id=w.owner.id, org_id=w.org.id)
    shouty = make_tag(w.db, name=f"{stem.upper()}_ALPHA", user_id=w.org_admin.id, org_id=w.org.id)
    attach(w.db, w.file, colleague)
    attach(w.db, w.file, shouty)

    def cluster_rights(headers) -> dict[str, bool]:
        response = w.client.get("/api/tags/collisions", headers=headers)
        assert response.status_code == 200, response.text
        return {
            member["uuid"]: member["can_write"]
            for cluster in response.json()
            for member in cluster["members"]
        }

    assert cluster_rights(w.org_admin_headers) == {
        str(colleague.uuid): True,
        str(shouty.uuid): True,
    }
    assert cluster_rights(w.owner_headers) == {
        str(colleague.uuid): True,
        str(shouty.uuid): False,
    }

    selection = w.client.get(
        "/api/tags/for-files", headers=w.owner_headers, params={"file_uuids": [str(w.file.uuid)]}
    )
    assert selection.status_code == 200, selection.text
    assert {row["uuid"]: row["can_write"] for row in selection.json()} == {
        str(colleague.uuid): True,
        str(shouty.uuid): False,
    }


def test_a_support_grant_reports_no_tag_writable(support_world):
    w = support_world
    shared = make_tag(w.db, name=_word(), user_id=None, org_id=None)
    assert _listed(w, w.super_headers, shared)["can_write"] is True

    grant = w.active_grant(headers=w.super_headers)
    under_grant = w.with_grant(grant, headers=w.super_headers)
    assert _listed(w, under_grant, shared)["can_write"] is False
    assert _rename(w, under_grant, shared) == 403
