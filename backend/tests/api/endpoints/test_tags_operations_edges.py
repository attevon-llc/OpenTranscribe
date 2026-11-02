"""Characterization of the rename / merge / delete / cleanup routes' edges.

Written before the tags backend rewrite. Tests whose name says ``pins_current_behaviour``
pin behaviour the owner has decided to change: D8 (merge silently drops share grants) and
D16 (404 details carry internal integer ids).
"""

from __future__ import annotations

import uuid

import pytest
from fixtures.tag_world import acting_as
from fixtures.tag_world import attach
from fixtures.tag_world import build_org_world
from fixtures.tag_world import make_file
from fixtures.tag_world import make_tag
from fixtures.tag_world import word

from app.models.sharing import TagShare
from app.models.tag import FileTag
from app.models.tag import Tag


class _RecordingTask:
    def __init__(self) -> None:
        self.calls: list[list[int]] = []

    def delay(self, file_ids):
        self.calls.append(list(file_ids))


@pytest.fixture
def reindex(monkeypatch):
    from app.tasks import search_file_field_tasks

    recorder = _RecordingTask()
    monkeypatch.setattr(search_file_field_tasks, "update_file_tags_index", recorder)
    return recorder


@pytest.fixture
def org_world(db_session):
    return build_org_world(db_session)


def _exists(db, tag: Tag | int) -> bool:
    tag_id = tag if isinstance(tag, int) else tag.id
    db.expire_all()
    return db.query(Tag).filter(Tag.id == tag_id).first() is not None


def _grant(db, tag: Tag, owner, target) -> TagShare:
    share = TagShare(
        tag_id=tag.id, shared_by_id=owner.id, target_type="user", target_user_id=target.id
    )
    db.add(share)
    db.commit()
    db.refresh(share)
    return share


def _shares(db, tag: Tag) -> int:
    db.expire_all()
    count: int = db.query(TagShare).filter(TagShare.tag_id == tag.id).count()
    return count


# ---------------------------------------------------------------------------
# Rename: blank names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blank", ["", "   ", "-", "__", " - _ "])
def test_rename_to_a_blank_name_is_a_422_and_changes_nothing(
    client, db_session, normal_user, user_token_headers, blank
):
    tag = make_tag(db_session, word("keepname"), user_id=normal_user.id)

    response = client.patch(
        f"/api/tags/{tag.uuid}", headers=user_token_headers, json={"name": blank}
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Tag name is required"
    db_session.refresh(tag)
    assert tag.name.startswith("keepname")


# ---------------------------------------------------------------------------
# Merge: tenants, and a merge that names only the target
# ---------------------------------------------------------------------------


def _super_in_org(w) -> dict[str, str]:
    """The super_admin acting inside ``w.org`` as an org admin (can write system tags)."""
    w.roles[w.super_admin.id] = (w.org.id, "org:admin")
    headers: dict[str, str] = w.super_headers
    return headers


def test_merging_a_system_tag_into_an_org_tag_is_a_422_and_deletes_nothing(support_world):
    w = support_world
    headers = _super_in_org(w)
    org_tag = make_tag(w.db, word("org-survivor"), user_id=w.owner.id, org_id=w.org.id)
    system = make_tag(w.db, word("sys-source"), user_id=None)

    response = w.client.post(
        f"/api/tags/{org_tag.uuid}/merge",
        headers=headers,
        json={"source_uuids": [str(system.uuid)]},
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "Tags from different tenants cannot be merged"
    assert _exists(w.db, org_tag)
    assert _exists(w.db, system)


def test_merging_an_org_tag_into_a_system_tag_is_allowed(support_world):
    w = support_world
    headers = _super_in_org(w)
    system = make_tag(w.db, word("sys-survivor"), user_id=None)
    org_tag = make_tag(w.db, word("org-source"), user_id=w.owner.id, org_id=w.org.id)

    response = w.client.post(
        f"/api/tags/{system.uuid}/merge",
        headers=headers,
        json={"source_uuids": [str(org_tag.uuid)]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["merged"] is True
    assert body["deleted_uuids"] == [str(org_tag.uuid)]
    assert _exists(w.db, system)
    assert not _exists(w.db, org_tag)


def test_merge_naming_only_the_target_is_a_no_op(
    client, db_session, normal_user, user_token_headers, reindex
):
    target = make_tag(db_session, word("solo"), user_id=normal_user.id)
    media_file = make_file(db_session, normal_user)
    attach(db_session, media_file, target)

    response = client.post(
        f"/api/tags/{target.uuid}/merge",
        headers=user_token_headers,
        json={"source_uuids": [str(target.uuid)]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["merged"] is False
    assert body["requires_confirmation"] is False
    assert body["deleted_uuids"] == []
    assert body["tag"]["uuid"] == str(target.uuid)
    assert body["impact"] == {"tags": [], "accessible_file_count": 0, "total_file_count": 0}
    assert _exists(db_session, target)
    assert db_session.query(FileTag).filter(FileTag.tag_id == target.id).count() == 1
    assert reindex.calls == []


def test_merge_with_an_unknown_source_uuid_is_a_404(
    client, db_session, normal_user, user_token_headers
):
    target = make_tag(db_session, word("t"), user_id=normal_user.id)

    response = client.post(
        f"/api/tags/{target.uuid}/merge",
        headers=user_token_headers,
        json={"source_uuids": [str(uuid.uuid4())]},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Tag not found"


def test_merge_with_an_empty_source_list_is_a_422(
    client, db_session, normal_user, user_token_headers
):
    target = make_tag(db_session, word("t"), user_id=normal_user.id)

    response = client.post(
        f"/api/tags/{target.uuid}/merge", headers=user_token_headers, json={"source_uuids": []}
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Share grants on deleted tags
# ---------------------------------------------------------------------------


def test_delete_removes_the_tags_share_grants(
    client, db_session, normal_user, other_user, user_token_headers
):
    tag = make_tag(db_session, word("shared-del"), user_id=normal_user.id)
    _grant(db_session, tag, normal_user, other_user)

    response = client.delete(
        "/api/tags", headers=user_token_headers, params={"tag_uuids": [str(tag.uuid)]}
    )

    assert response.status_code == 200, response.text
    assert _shares(db_session, tag) == 0


def test_merge_drops_the_doomed_tags_grants_without_moving_them_pins_current_behaviour(
    client, db_session, normal_user, other_user, user_token_headers
):
    """D8: the recipient of the doomed tag's grant silently loses access, and the survivor
    gains no grant."""
    survivor = make_tag(db_session, word("survivor"), user_id=normal_user.id)
    doomed = make_tag(db_session, word("doomed"), user_id=normal_user.id)
    _grant(db_session, doomed, normal_user, other_user)

    response = client.post(
        f"/api/tags/{survivor.uuid}/merge",
        headers=user_token_headers,
        json={"source_uuids": [str(doomed.uuid)]},
    )

    assert response.status_code == 200, response.text
    assert _shares(db_session, doomed) == 0
    assert _shares(db_session, survivor) == 0
    assert db_session.query(TagShare).filter(TagShare.target_user_id == other_user.id).count() == 0


def test_merge_leaves_the_survivors_own_grants_alone(
    client, db_session, normal_user, other_user, user_token_headers
):
    survivor = make_tag(db_session, word("survivor"), user_id=normal_user.id)
    doomed = make_tag(db_session, word("doomed"), user_id=normal_user.id)
    _grant(db_session, survivor, normal_user, other_user)

    client.post(
        f"/api/tags/{survivor.uuid}/merge",
        headers=user_token_headers,
        json={"source_uuids": [str(doomed.uuid)]},
    )

    assert _shares(db_session, survivor) == 1


# ---------------------------------------------------------------------------
# D16: 404 details from TagNotFoundError carry internal integer ids
# ---------------------------------------------------------------------------


@pytest.fixture
def vanishing_rows(monkeypatch):
    """Make the row lock find nothing, as if a concurrent request deleted the tags."""
    monkeypatch.setattr("app.services.tag_operations.lock_tags", lambda db, tag_ids: {})


def test_delete_404_detail_carries_the_internal_id_pins_current_behaviour(
    client, db_session, normal_user, user_token_headers, vanishing_rows
):
    """D16: the integer primary key reaches the wire."""
    tag = make_tag(db_session, word("gone"), user_id=normal_user.id)

    response = client.delete(
        "/api/tags", headers=user_token_headers, params={"tag_uuids": [str(tag.uuid)]}
    )

    assert response.status_code == 404
    assert response.json()["detail"] == f"Tags no longer exist: [{tag.id}]"


def test_rename_404_detail_carries_the_internal_id_pins_current_behaviour(
    client, db_session, normal_user, user_token_headers, vanishing_rows
):
    """D16."""
    tag = make_tag(db_session, word("gone"), user_id=normal_user.id)

    response = client.patch(
        f"/api/tags/{tag.uuid}", headers=user_token_headers, json={"name": word("n")}
    )

    assert response.status_code == 404
    assert response.json()["detail"] == f"Tag {tag.id} no longer exists"


def test_merge_404_detail_carries_the_internal_id_pins_current_behaviour(
    client, db_session, normal_user, user_token_headers, vanishing_rows
):
    """D16: the target is looked up first, so its id is the one reported."""
    target = make_tag(db_session, word("t"), user_id=normal_user.id)
    source = make_tag(db_session, word("s"), user_id=normal_user.id)

    response = client.post(
        f"/api/tags/{target.uuid}/merge",
        headers=user_token_headers,
        json={"source_uuids": [str(source.uuid)]},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == f"Tag {target.id} no longer exists"


def test_promote_404_detail_carries_the_internal_id_pins_current_behaviour(
    client, db_session, admin_user, admin_token_headers, vanishing_rows, monkeypatch
):
    """D16, promote path (single-tenant so a plain admin may promote)."""
    from app.core.config import settings
    from app.services.platform_access import reset_tenancy_mode_cache

    monkeypatch.setattr(settings, "TENANCY_MODE", "single")
    reset_tenancy_mode_cache()
    try:
        tag = make_tag(db_session, word("gone"), user_id=admin_user.id)

        response = client.post(
            "/api/tags/promote", headers=admin_token_headers, json={"tag_uuids": [str(tag.uuid)]}
        )
    finally:
        reset_tenancy_mode_cache()

    assert response.status_code == 404
    assert response.json()["detail"] == f"Tags no longer exist: [{tag.id}]"


# ---------------------------------------------------------------------------
# Cleanup inside an organization
# ---------------------------------------------------------------------------


def test_cleanup_in_an_org_deletes_only_the_callers_own_unreferenced_org_tags(support_world):
    w = support_world
    w.roles[w.admin.id] = (w.org.id, "org:admin")
    mine_in_org = make_tag(w.db, word("mine-org"), user_id=w.admin.id, org_id=w.org.id)
    colleagues = make_tag(w.db, word("colleague-org"), user_id=w.owner.id, org_id=w.org.id)
    unattributed = make_tag(w.db, word("orphan-org"), user_id=None, org_id=w.org.id)
    mine_in_other_org = make_tag(
        w.db, word("mine-other"), user_id=w.admin.id, org_id=w.other_org.id
    )
    mine_personal = make_tag(w.db, word("mine-personal"), user_id=w.admin.id)
    referenced = make_tag(w.db, word("referenced"), user_id=w.admin.id, org_id=w.org.id)
    attach(w.db, w.file, referenced)
    doomed_id = mine_in_org.id
    kept_ids = [
        t.id for t in (colleagues, unattributed, mine_in_other_org, mine_personal, referenced)
    ]

    response = w.client.delete("/api/tags/cleanup", headers=w.admin_headers)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "deleted_count": 1,
        "scope": "mine",
        "message": "1 unused tags deleted successfully",
    }
    assert not _exists(w.db, doomed_id)
    assert all(_exists(w.db, kept) for kept in kept_ids)


# ---------------------------------------------------------------------------
# D3: ownership on TagMutationResult.tag
# ---------------------------------------------------------------------------


def test_held_rename_onto_a_colleagues_org_tag_reports_it_as_mine_pins_current_behaviour(
    client, org_world
):
    """D3: the colliding row is bob's, yet the bare-ORM label for alice is ``mine``."""
    w = org_world
    bobs = make_tag(w.db, word("bobs-word"), user_id=w.bob.id, org_id=w.org_a.id)
    alices = make_tag(w.db, word("alices-word"), user_id=w.alice.id, org_id=w.org_a.id)

    with acting_as(w.alice, w.org_a.id, "org:admin"):
        response = client.patch(f"/api/tags/{alices.uuid}", json={"name": bobs.name})
        listed = {r["uuid"]: r["ownership"] for r in client.get("/api/tags").json()}

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["requires_confirmation"] is True
    assert body["tag"]["uuid"] == str(bobs.uuid)
    assert body["tag"]["ownership"] == "mine"
    assert listed[str(bobs.uuid)] == "shared_with_me"


def test_held_rename_onto_an_unattributed_org_tag_reports_it_as_system_pins_current_behaviour(
    client, org_world
):
    """D3: an org tag with no creator is labelled ``system`` here, ``shared_with_me`` in the list."""
    w = org_world
    orphan = make_tag(w.db, word("orphan-word"), user_id=None, org_id=w.org_a.id)
    alices = make_tag(w.db, word("alices-word"), user_id=w.alice.id, org_id=w.org_a.id)

    with acting_as(w.alice, w.org_a.id, "org:admin"):
        response = client.patch(f"/api/tags/{alices.uuid}", json={"name": orphan.name})
        listed = {r["uuid"]: r["ownership"] for r in client.get("/api/tags").json()}

    assert response.status_code == 200, response.text
    assert response.json()["tag"]["uuid"] == str(orphan.uuid)
    assert response.json()["tag"]["ownership"] == "system"
    assert listed[str(orphan.uuid)] == "shared_with_me"


def test_a_plain_rename_reports_the_renamed_row_with_its_new_name(
    client, db_session, normal_user, user_token_headers
):
    tag = make_tag(db_session, word("before"), user_id=normal_user.id)
    new_name = word("after")

    body = client.patch(
        f"/api/tags/{tag.uuid}", headers=user_token_headers, json={"name": new_name}
    ).json()

    assert body["merged"] is False and body["requires_confirmation"] is False
    assert body["tag"] == {
        "uuid": str(tag.uuid),
        "name": new_name,
        "source": "manual",
        "ownership": "mine",
    }
    assert body["deleted_uuids"] == []
