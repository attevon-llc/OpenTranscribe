"""Writes to the shared tag vocabulary and deployment-wide tag sweeps follow the tenancy mode.

A multi-tenant deployment gives a plain platform admin no implicit reach into a tenant, and
the shared (system) vocabulary is read by every tenant. So there the shared-vocabulary routes
and the deployment-wide sweep need a ``super_admin``; promotion folds only the promoted tag's
own tenant. A single-tenant deployment keeps instance-wide behaviour for every admin.

The controls are in the same tests: the super_admin (multi) and the plain admin (single) are
served by the very request the plain admin (multi) is refused.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from fixtures.tenant_rows import make_tag

from app.core.config import settings
from app.models.media import Tag
from app.services.platform_access import reset_tenancy_mode_cache


@pytest.fixture
def single_tenant(support_world, monkeypatch):
    """Flip the world to a single-tenant deployment (organizations exist but are ignored)."""
    monkeypatch.setattr(settings, "TENANCY_MODE", "single")
    reset_tenancy_mode_cache()
    yield support_world
    reset_tenancy_mode_cache()


def _word() -> str:
    return f"vocab{uuid.uuid4().hex[:8]}"


def _name(db, tag: Tag) -> str | None:
    db.expire_all()
    row = db.query(Tag).filter(Tag.id == tag.id).first()
    return None if row is None else str(row.name)


def _exists(db, tag: Tag) -> bool:
    return _name(db, tag) is not None


def test_a_plain_admin_cannot_rename_a_system_tag_in_a_multi_tenant_deployment(support_world):
    w = support_world
    shared = make_tag(w.db, name=_word(), user_id=None, org_id=None)
    renamed = f"{shared.name}x"

    refused = w.client.patch(
        f"/api/tags/{shared.uuid}", headers=w.admin_headers, json={"name": renamed}
    )
    assert refused.status_code == 404, refused.text
    assert _name(w.db, shared) == shared.name

    served = w.client.patch(
        f"/api/tags/{shared.uuid}", headers=w.super_headers, json={"name": renamed}
    )
    assert served.status_code == 200, served.text
    assert _name(w.db, shared) == renamed


def test_a_plain_admin_cannot_delete_or_size_a_system_tag_in_a_multi_tenant_deployment(
    support_world,
):
    w = support_world
    shared = make_tag(w.db, name=_word(), user_id=None, org_id=None)
    params = {"tag_uuids": [str(shared.uuid)]}

    assert (
        w.client.get("/api/tags/impact", headers=w.admin_headers, params=params).status_code == 404
    )
    refused = w.client.delete("/api/tags", headers=w.admin_headers, params=params)
    assert refused.status_code == 404, refused.text
    assert _exists(w.db, shared)

    assert (
        w.client.get("/api/tags/impact", headers=w.super_headers, params=params).status_code == 200
    )
    served = w.client.delete("/api/tags", headers=w.super_headers, params=params)
    assert served.status_code == 200, served.text
    assert not _exists(w.db, shared)


def test_a_plain_admin_still_manages_system_tags_in_a_single_tenant_deployment(single_tenant):
    w = single_tenant
    shared = make_tag(w.db, name=_word(), user_id=None, org_id=None)
    renamed = f"{shared.name}x"
    served = w.client.patch(
        f"/api/tags/{shared.uuid}", headers=w.admin_headers, json={"name": renamed}
    )
    assert served.status_code == 200, served.text
    assert _name(w.db, shared) == renamed


def test_a_plain_admin_cannot_promote_in_a_multi_tenant_deployment(support_world):
    w = support_world
    mine = make_tag(w.db, name=_word(), user_id=w.admin.id, org_id=None)
    refused = w.client.post(
        "/api/tags/promote", headers=w.admin_headers, json={"tag_uuids": [str(mine.uuid)]}
    )
    assert refused.status_code == 403, refused.text
    w.db.expire_all()
    assert w.db.get(Tag, mine.id).user_id == w.admin.id


def test_promotion_by_a_super_admin_does_not_fold_another_tenants_tags(support_world):
    w = support_world
    word = _word()
    mine = make_tag(w.db, name=word, user_id=w.super_admin.id, org_id=None)
    in_org = make_tag(w.db, name=word, user_id=w.org_admin.id, org_id=w.org.id)
    in_other_org = make_tag(w.db, name=word, user_id=w.owner.id, org_id=w.other_org.id)
    colleague = make_tag(w.db, name=word, user_id=w.owner.id, org_id=None)

    response = w.client.post(
        "/api/tags/promote", headers=w.super_headers, json={"tag_uuids": [str(mine.uuid)]}
    )
    assert response.status_code == 200, response.text
    w.db.expire_all()
    promoted = w.db.get(Tag, mine.id)
    assert promoted is not None
    assert promoted.user_id is None and promoted.organization_id is None, "the control: promoted"
    for other in (in_org, in_other_org, colleague):
        assert _exists(w.db, other), "another tenant's tag was folded into the shared one"


def test_promotion_still_folds_the_same_tenant_duplicates_in_a_multi_tenant_deployment(
    support_world,
):
    """The fold is narrowed to the tag's tenant, not removed."""
    w = support_world
    word = _word()
    mine = make_tag(w.db, name=word, user_id=w.super_admin.id, org_id=None)
    sibling = make_tag(w.db, name=word.upper(), user_id=w.super_admin.id, org_id=None)
    # Same normalized name, same tenant: only reachable through a legacy row, so give it a
    # distinct display name and the shared normalization.
    sibling.normalized_name = mine.normalized_name
    w.db.commit()

    response = w.client.post(
        "/api/tags/promote", headers=w.super_headers, json={"tag_uuids": [str(mine.uuid)]}
    )
    assert response.status_code == 200, response.text
    assert not _exists(w.db, sibling)


def test_promotion_folds_every_tenant_in_a_single_tenant_deployment(single_tenant):
    w = single_tenant
    word = _word()
    mine = make_tag(w.db, name=word, user_id=w.admin.id, org_id=None)
    elsewhere = make_tag(w.db, name=word, user_id=w.owner.id, org_id=None)
    in_org = make_tag(w.db, name=word, user_id=w.org_admin.id, org_id=w.org.id)

    response = w.client.post(
        "/api/tags/promote", headers=w.admin_headers, json={"tag_uuids": [str(mine.uuid)]}
    )
    assert response.status_code == 200, response.text
    assert not _exists(w.db, elsewhere)
    assert not _exists(w.db, in_org)


CLEANUP = "/api/tags/cleanup"
SWEEP = "app.api.endpoints.tags.crud.cleanup_unreferenced_tags"


def test_the_deployment_wide_sweep_needs_a_super_admin_in_a_multi_tenant_deployment(
    support_world,
):
    w = support_world
    params = {"scope": "all_users", "confirm": "true"}
    with patch(SWEEP, return_value=0) as sweep:
        refused = w.client.delete(CLEANUP, headers=w.admin_headers, params=params)
        assert refused.status_code == 403, refused.text
        assert not sweep.called, "the sweep ran for a caller who was refused"

        mine = w.client.delete(CLEANUP, headers=w.admin_headers)
        assert mine.status_code == 200, mine.text
        assert sweep.call_args.kwargs["all_users"] is False

        served = w.client.delete(CLEANUP, headers=w.super_headers, params=params)
        assert served.status_code == 200, served.text
        assert sweep.call_args.kwargs["all_users"] is True


def test_the_deployment_wide_sweep_stays_open_to_an_admin_in_a_single_tenant_deployment(
    single_tenant,
):
    w = single_tenant
    with patch(SWEEP, return_value=0) as sweep:
        served = w.client.delete(
            CLEANUP, headers=w.admin_headers, params={"scope": "all_users", "confirm": "true"}
        )
    assert served.status_code == 200, served.text
    assert sweep.call_args.kwargs["all_users"] is True


def test_an_unreferenced_tenant_tag_survives_a_refused_sweep(support_world):
    w = support_world
    unused = make_tag(w.db, name=_word(), user_id=w.owner.id, org_id=w.other_org.id)
    refused = w.client.delete(
        CLEANUP, headers=w.admin_headers, params={"scope": "all_users", "confirm": "true"}
    )
    assert refused.status_code == 403, refused.text
    assert _exists(w.db, unused)
