"""Tag sharing stays inside the tag's tenant, and the shared vocabulary follows the tenancy mode.

Two rules, each proved from both sides in the same test (the allowed case is the control):

* A tag can be shared with a user or group of the tag's own tenant. An organization's tag with
  that organization's members, a personal tag (the personal workspace is its own tenant in a
  multi-tenant deployment) with organization-less users only. A single-tenant deployment has one
  vocabulary and keeps sharing with every account.
* Managing the shares of a shared (system) tag, and merging a system tag in or out, needs the
  deployment-scope right: a ``super_admin`` in a multi-tenant deployment, any admin otherwise.
"""

from __future__ import annotations

import uuid

import pytest
from fixtures.support_world import make_user
from fixtures.tenant_rows import make_tag

from app.core.config import settings
from app.models.group import UserGroup
from app.models.group import UserGroupMember
from app.models.media import Tag
from app.models.organization import OrganizationMembership
from app.models.sharing import TagShare
from app.services.platform_access import reset_tenancy_mode_cache


@pytest.fixture
def single_tenant(support_world, monkeypatch):
    monkeypatch.setattr(settings, "TENANCY_MODE", "single")
    reset_tenancy_mode_cache()
    yield support_world
    reset_tenancy_mode_cache()


def _word() -> str:
    return f"share{uuid.uuid4().hex[:8]}"


def _shares(db, tag: Tag) -> list[TagShare]:
    db.expire_all()
    rows: list[TagShare] = db.query(TagShare).filter(TagShare.tag_id == tag.id).all()
    return rows


def _share(w, tag: Tag, *, headers, user=None, group=None):
    body = {}
    if user is not None:
        body["target_user_uuid"] = str(user.uuid)
    if group is not None:
        body["target_group_uuid"] = str(group.uuid)
    return w.client.post(f"/api/tags/{tag.uuid}/shares", headers=headers, json=body)


def _org_member(w, label: str):
    member = make_user(w.db, role="user", label=label)
    w.db.add(OrganizationMembership(organization_id=w.org.id, user_id=member.id, role="org:member"))
    w.db.commit()
    return member


def _group(w, owner, members) -> UserGroup:
    group = UserGroup(name=f"zz-{uuid.uuid4().hex[:8]}", owner_id=owner.id)
    w.db.add(group)
    w.db.commit()
    for user in (owner, *members):
        w.db.add(UserGroupMember(group_id=group.id, user_id=user.id, role="member"))
    w.db.commit()
    return group


# --- a personal tag stays inside the personal workspace -------------------------------------


def test_a_personal_tag_is_not_shared_with_an_organization_member(support_world):
    w = support_world
    tag = make_tag(w.db, name=_word(), user_id=w.owner.id, org_id=None)
    colleague = make_user(w.db, role="user", label="personal")
    member = _org_member(w, "orgmember")

    refused = _share(w, tag, headers=w.owner_headers, user=member)
    assert refused.status_code == 422, refused.text
    assert _shares(w.db, tag) == []

    served = _share(w, tag, headers=w.owner_headers, user=colleague)
    assert served.status_code == 200, served.text
    assert [s.target_user_id for s in _shares(w.db, tag)] == [colleague.id]


def test_a_personal_tag_is_not_shared_with_a_group_that_has_an_organization_member(support_world):
    w = support_world
    tag = make_tag(w.db, name=_word(), user_id=w.owner.id, org_id=None)
    plain = _group(w, w.owner, [make_user(w.db, role="user", label="plain")])
    mixed = _group(w, w.owner, [_org_member(w, "mixedmember")])

    refused = _share(w, tag, headers=w.owner_headers, group=mixed)
    assert refused.status_code == 422, refused.text
    assert _shares(w.db, tag) == []

    served = _share(w, tag, headers=w.owner_headers, group=plain)
    assert served.status_code == 200, served.text
    assert [s.target_group_id for s in _shares(w.db, tag)] == [plain.id]


def test_a_single_tenant_deployment_shares_a_personal_tag_with_every_account(single_tenant):
    w = single_tenant
    tag = make_tag(w.db, name=_word(), user_id=w.owner.id, org_id=None)
    member = _org_member(w, "singlemember")

    served = _share(w, tag, headers=w.owner_headers, user=member)
    assert served.status_code == 200, served.text
    assert [s.target_user_id for s in _shares(w.db, tag)] == [member.id]


def test_an_organization_tag_still_shares_only_within_the_organization(support_world):
    w = support_world
    tag = make_tag(w.db, name=_word(), user_id=w.org_admin.id, org_id=w.org.id)
    member = _org_member(w, "insider")
    outsider = make_user(w.db, role="user", label="outsider")

    refused = _share(w, tag, headers=w.org_admin_headers, user=outsider)
    assert refused.status_code == 422, refused.text

    served = _share(w, tag, headers=w.org_admin_headers, user=member)
    assert served.status_code == 200, served.text


# --- the shared vocabulary's share and merge routes need the deployment-scope right ---------


def test_only_a_super_admin_lists_the_shares_of_a_system_tag_in_a_multi_tenant_deployment(
    support_world,
):
    w = support_world
    shared = make_tag(w.db, name=_word(), user_id=None, org_id=None)
    path = f"/api/tags/{shared.uuid}/shares"
    assert w.client.get(path, headers=w.admin_headers).status_code == 404
    assert w.client.get(path, headers=w.super_headers).status_code == 200


def test_only_a_super_admin_creates_a_share_of_a_system_tag_in_a_multi_tenant_deployment(
    support_world,
):
    w = support_world
    shared = make_tag(w.db, name=_word(), user_id=None, org_id=None)
    refused = _share(w, shared, headers=w.admin_headers, user=w.owner)
    assert refused.status_code == 404, refused.text
    assert _shares(w.db, shared) == []

    served = _share(w, shared, headers=w.super_headers, user=w.owner)
    assert served.status_code == 200, served.text
    assert len(_shares(w.db, shared)) == 1


def test_only_a_super_admin_revokes_a_share_of_a_system_tag_in_a_multi_tenant_deployment(
    support_world,
):
    w = support_world
    shared = make_tag(w.db, name=_word(), user_id=None, org_id=None)
    grant = TagShare(
        tag_id=shared.id,
        shared_by_id=w.super_admin.id,
        target_type="user",
        target_user_id=w.owner.id,
    )
    w.db.add(grant)
    w.db.commit()
    path = f"/api/tags/{shared.uuid}/shares/{grant.uuid}"

    assert w.client.delete(path, headers=w.admin_headers).status_code == 404
    assert len(_shares(w.db, shared)) == 1

    assert w.client.delete(path, headers=w.super_headers).status_code == 204
    assert _shares(w.db, shared) == []


def test_a_plain_admin_manages_the_shares_of_a_system_tag_in_a_single_tenant_deployment(
    single_tenant,
):
    w = single_tenant
    shared = make_tag(w.db, name=_word(), user_id=None, org_id=None)
    assert _share(w, shared, headers=w.admin_headers, user=w.owner).status_code == 200
    assert (
        w.client.get(f"/api/tags/{shared.uuid}/shares", headers=w.admin_headers).status_code == 200
    )


def _exists(db, tag: Tag) -> bool:
    db.expire_all()
    return db.get(Tag, tag.id) is not None


def test_a_plain_admin_cannot_merge_into_a_system_tag_in_a_multi_tenant_deployment(support_world):
    """The survivor (path) site: the source is the admin's own tag, so only the target is shared."""
    w = support_world
    survivor = make_tag(w.db, name=_word(), user_id=None, org_id=None)
    mine = make_tag(w.db, name=_word(), user_id=w.admin.id, org_id=None)
    body = {"source_uuids": [str(mine.uuid)]}

    refused = w.client.post(f"/api/tags/{survivor.uuid}/merge", headers=w.admin_headers, json=body)
    assert refused.status_code == 404, refused.text
    assert _exists(w.db, mine)

    own_survivor = make_tag(w.db, name=_word(), user_id=w.admin.id, org_id=None)
    served = w.client.post(
        f"/api/tags/{own_survivor.uuid}/merge", headers=w.admin_headers, json=body
    )
    assert served.status_code == 200, served.text
    assert not _exists(w.db, mine)


def test_a_plain_admin_cannot_merge_a_system_tag_away_in_a_multi_tenant_deployment(support_world):
    """The source site: the survivor is the admin's own tag, so only the source is shared."""
    w = support_world
    shared = make_tag(w.db, name=_word(), user_id=None, org_id=None)
    mine = make_tag(w.db, name=_word(), user_id=w.admin.id, org_id=None)

    refused = w.client.post(
        f"/api/tags/{mine.uuid}/merge",
        headers=w.admin_headers,
        json={"source_uuids": [str(shared.uuid)]},
    )
    assert refused.status_code == 404, refused.text
    assert _exists(w.db, shared)


def test_a_super_admin_merges_system_tags_in_a_multi_tenant_deployment(support_world):
    w = support_world
    survivor = make_tag(w.db, name=_word(), user_id=None, org_id=None)
    doomed = make_tag(w.db, name=_word(), user_id=None, org_id=None)
    served = w.client.post(
        f"/api/tags/{survivor.uuid}/merge",
        headers=w.super_headers,
        json={"source_uuids": [str(doomed.uuid)]},
    )
    assert served.status_code == 200, served.text
    assert not _exists(w.db, doomed)


def test_a_plain_admin_merges_system_tags_in_a_single_tenant_deployment(single_tenant):
    w = single_tenant
    survivor = make_tag(w.db, name=_word(), user_id=None, org_id=None)
    doomed = make_tag(w.db, name=_word(), user_id=None, org_id=None)
    served = w.client.post(
        f"/api/tags/{survivor.uuid}/merge",
        headers=w.admin_headers,
        json={"source_uuids": [str(doomed.uuid)]},
    )
    assert served.status_code == 200, served.text
    assert not _exists(w.db, doomed)
