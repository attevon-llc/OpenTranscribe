"""``POST /api/prompts/shared/{uuid}/toggle`` acts only inside the caller's tenant.

The owner may always toggle their own prompt. An admin may toggle a prompt another user owns
only when that prompt belongs to the admin's own tenant (the owner is a member of the active
organization, or of none for the personal scope). A prompt in another tenant answers 404 so
its existence is not disclosed, for an admin of another organization and for a platform admin
or super_admin who holds no support-access grant, and nothing about it is written.

Real routes, real bearer tokens, the tenancy world from ``fixtures.support_world``.
"""

from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime

import pytest
from fixtures.support_world import login_headers
from fixtures.support_world import make_user

from app.core.config import settings
from app.models.organization import OrganizationMembership
from app.models.prompt import SummaryPrompt
from app.models.prompt import UserSetting
from app.services.platform_access import reset_tenancy_mode_cache


def _prompt(db, owner, *, shared: bool = False) -> SummaryPrompt:
    row = SummaryPrompt(
        user_id=owner.id,
        name=f"zz-scope-{uuid.uuid4().hex[:8]}",
        prompt_text="Summarize.",
        is_system_default=False,
        is_active=True,
        is_shared=shared,
        shared_at=datetime.now(UTC) if shared else None,
        shared_by=owner.id if shared else None,
        content_type="general",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _toggle(client, headers, prompt, *, shared: bool):
    return client.post(
        f"/api/prompts/shared/{prompt.uuid}/toggle", headers=headers, json={"is_shared": shared}
    )


def _snapshot(db, prompt) -> tuple:
    db.expire_all()
    row = db.query(SummaryPrompt).filter(SummaryPrompt.id == prompt.id).one()
    return (row.is_shared, row.shared_at, row.shared_by)


@pytest.fixture
def tenants(support_world):
    """An owner and an admin inside each organization, plus the personal owner/admins."""
    w = support_world
    db = w.db
    members = {}
    for label, org in (("a", w.org), ("b", w.other_org)):
        owner = make_user(db, role="user", label=f"own{label}")
        admin = make_user(db, role="admin", label=f"adm{label}")
        for user in (owner, admin):
            db.add(
                OrganizationMembership(organization_id=org.id, user_id=user.id, role="org:member")
            )
            w.roles[int(user.id)] = (org.id, "org:member")
        members[label] = (owner, admin)
    db.commit()
    return members


def test_the_owner_can_toggle_their_own_prompt(support_world, tenants):
    w = support_world
    owner_a, _ = tenants["a"]
    prompt = _prompt(w.db, owner_a)
    response = _toggle(w.client, login_headers(w.client, owner_a), prompt, shared=True)
    assert response.status_code == 200, response.text
    assert _snapshot(w.db, prompt)[0] is True


def test_an_org_admin_can_toggle_a_members_prompt(support_world, tenants):
    w = support_world
    owner_a, _ = tenants["a"]
    org_admin = make_user(w.db, role="user", label="orgadm")
    w.db.add(
        OrganizationMembership(organization_id=w.org.id, user_id=org_admin.id, role="org:admin")
    )
    w.db.commit()
    w.roles[int(org_admin.id)] = (w.org.id, "org:admin")
    prompt = _prompt(w.db, owner_a)
    response = _toggle(w.client, login_headers(w.client, org_admin), prompt, shared=True)
    assert response.status_code == 200, response.text
    is_shared, _, shared_by = _snapshot(w.db, prompt)
    assert is_shared is True
    assert shared_by == org_admin.id


@pytest.mark.parametrize("shared", [True, False])
def test_a_platform_admin_who_is_only_an_org_member_cannot_toggle_a_members_prompt(
    support_world, tenants, shared
):
    """Authority inside an organization comes from the org role or a write grant, not the platform role."""
    w = support_world
    owner_a, admin_a = tenants["a"]
    prompt = _prompt(w.db, owner_a, shared=not shared)
    before = _snapshot(w.db, prompt)
    response = _toggle(w.client, login_headers(w.client, admin_a), prompt, shared=shared)
    assert response.status_code == 403, response.text
    assert _snapshot(w.db, prompt) == before


@pytest.mark.parametrize("shared", [True, False])
def test_an_admin_of_another_tenant_gets_404_and_nothing_changes(support_world, tenants, shared):
    w = support_world
    owner_a, _ = tenants["a"]
    _, admin_b = tenants["b"]
    prompt = _prompt(w.db, owner_a, shared=not shared)
    # someone else has the prompt selected; an unshare must not clear that
    w.db.add(
        UserSetting(
            user_id=admin_b.id,
            setting_key="active_summary_prompt_id",
            setting_value=str(prompt.id),
        )
    )
    w.db.commit()
    before = _snapshot(w.db, prompt)

    response = _toggle(w.client, login_headers(w.client, admin_b), prompt, shared=shared)

    assert response.status_code == 404, response.text
    assert str(prompt.uuid) not in response.text
    assert _snapshot(w.db, prompt) == before
    kept = (
        w.db.query(UserSetting)
        .filter(
            UserSetting.setting_key == "active_summary_prompt_id",
            UserSetting.setting_value == str(prompt.id),
        )
        .count()
    )
    assert kept == 1


def test_an_admin_cannot_reach_a_personal_prompt_from_an_organization(support_world, tenants):
    w = support_world
    _, admin_a = tenants["a"]
    prompt = _prompt(w.db, w.owner)  # w.owner belongs to no organization
    before = _snapshot(w.db, prompt)
    response = _toggle(w.client, login_headers(w.client, admin_a), prompt, shared=True)
    assert response.status_code == 404, response.text
    assert _snapshot(w.db, prompt) == before


@pytest.mark.parametrize("who", ["admin", "super_admin"])
def test_a_platform_role_without_a_grant_gets_404_across_tenants(support_world, tenants, who):
    w = support_world
    owner_a, _ = tenants["a"]
    prompt = _prompt(w.db, owner_a)
    before = _snapshot(w.db, prompt)
    headers = w.admin_headers if who == "admin" else w.super_headers
    response = _toggle(w.client, headers, prompt, shared=True)
    assert response.status_code == 404, response.text
    assert _snapshot(w.db, prompt) == before


def test_a_write_grant_assumes_only_the_granted_tenant(support_world, tenants):
    w = support_world
    owner_a, _ = tenants["a"]
    in_grant = _prompt(w.db, owner_a)
    outside = _prompt(w.db, w.owner)  # a personal-scope owner is not in the granted tenant
    grant = w.active_grant(level="write", org=w.org)

    allowed = _toggle(w.client, w.with_grant(grant), in_grant, shared=True)
    assert allowed.status_code == 200, allowed.text
    assert _snapshot(w.db, in_grant)[0] is True

    before = _snapshot(w.db, outside)
    refused = _toggle(w.client, w.with_grant(grant), outside, shared=True)
    assert refused.status_code == 404, refused.text
    assert _snapshot(w.db, outside) == before


def test_a_read_grant_cannot_toggle(support_world, tenants):
    w = support_world
    owner_a, _ = tenants["a"]
    prompt = _prompt(w.db, owner_a)
    before = _snapshot(w.db, prompt)
    grant = w.active_grant(level="read", org=w.org)
    response = _toggle(w.client, w.with_grant(grant), prompt, shared=True)
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "support_grant_write_required"
    assert _snapshot(w.db, prompt) == before


def test_a_member_who_is_not_an_admin_still_gets_403_inside_the_tenant(support_world, tenants):
    w = support_world
    owner_a, _ = tenants["a"]
    peer = make_user(w.db, role="user", label="peer")
    w.db.add(OrganizationMembership(organization_id=w.org.id, user_id=peer.id, role="org:member"))
    w.db.commit()
    w.roles[int(peer.id)] = (w.org.id, "org:member")
    prompt = _prompt(w.db, owner_a)
    before = _snapshot(w.db, prompt)
    response = _toggle(w.client, login_headers(w.client, peer), prompt, shared=True)
    assert response.status_code == 403, response.text
    assert _snapshot(w.db, prompt) == before


def test_a_user_outside_the_tenant_cannot_tell_the_prompt_exists(support_world, tenants):
    w = support_world
    owner_a, _ = tenants["a"]
    prompt = _prompt(w.db, owner_a)
    response = _toggle(w.client, w.owner_headers, prompt, shared=True)
    assert response.status_code == 404, response.text
    missing = w.client.post(
        f"/api/prompts/shared/{uuid.uuid4()}/toggle",
        headers=w.owner_headers,
        json={"is_shared": True},
    )
    assert missing.status_code == 404
    assert missing.json() == response.json()


def test_a_single_tenant_admin_toggles_any_users_prompt(
    db_session, client, monkeypatch, normal_user, admin_user, admin_token_headers
):
    monkeypatch.setattr(settings, "TENANCY_MODE", "single")
    reset_tenancy_mode_cache()
    try:
        prompt = _prompt(db_session, normal_user)
        response = _toggle(client, admin_token_headers, prompt, shared=True)
        assert response.status_code == 200, response.text
        is_shared, _, shared_by = _snapshot(db_session, prompt)
        assert is_shared is True
        assert shared_by == admin_user.id
    finally:
        reset_tenancy_mode_cache()


@pytest.mark.parametrize("who", ["admin", "super_admin"])
def test_a_platform_role_without_a_grant_cannot_toggle_a_personal_prompt(
    support_world, tenants, who
):
    """A personal workspace is its own tenant: its prompts answer 404 to a stranger."""
    w = support_world
    prompt = _prompt(w.db, w.owner)  # w.owner belongs to no organization
    before = _snapshot(w.db, prompt)
    headers = w.admin_headers if who == "admin" else w.super_headers
    response = _toggle(w.client, headers, prompt, shared=True)
    assert response.status_code == 404, response.text
    assert str(prompt.uuid) not in response.text
    assert _snapshot(w.db, prompt) == before


def test_another_personal_user_gets_404_for_a_personal_prompt(support_world, tenants):
    w = support_world
    stranger = make_user(w.db, role="user", label="stranger")
    prompt = _prompt(w.db, w.owner)
    before = _snapshot(w.db, prompt)
    response = _toggle(w.client, login_headers(w.client, stranger), prompt, shared=True)
    assert response.status_code == 404, response.text
    assert _snapshot(w.db, prompt) == before


def test_a_personal_write_grant_reaches_only_the_granted_users_prompt(support_world, tenants):
    w = support_world
    granted = _prompt(w.db, w.owner)
    stranger = make_user(w.db, role="user", label="other")
    elsewhere = _prompt(w.db, stranger)
    grant = w.request(level="write", subject_user_uuid=str(w.owner.uuid))["uuid"]
    approved = w.client.post(
        f"/api/users/me/support-access/{grant}/approve", headers=w.owner_headers, json={}
    )
    assert approved.status_code == 200, approved.text

    allowed = _toggle(w.client, w.with_grant(grant), granted, shared=True)
    assert allowed.status_code == 200, allowed.text
    assert _snapshot(w.db, granted)[0] is True

    before = _snapshot(w.db, elsewhere)
    refused = _toggle(w.client, w.with_grant(grant), elsewhere, shared=True)
    assert refused.status_code == 404, refused.text
    assert _snapshot(w.db, elsewhere) == before


def test_the_owner_can_still_toggle_a_personal_prompt_in_a_multi_tenant_deployment(
    support_world, tenants
):
    w = support_world
    prompt = _prompt(w.db, w.owner)
    response = _toggle(w.client, w.owner_headers, prompt, shared=True)
    assert response.status_code == 200, response.text
    assert _snapshot(w.db, prompt)[0] is True


def test_a_prompt_stamped_for_another_organization_is_out_of_tenant_for_an_org_admin(
    support_world, tenants
):
    """An owner in two organizations: a prompt stamped for one is not the other's to administer."""
    w = support_world
    owner_a, _ = tenants["a"]
    w.db.add(
        OrganizationMembership(
            organization_id=w.other_org.id, user_id=owner_a.id, role="org:member"
        )
    )
    org_admin = make_user(w.db, role="user", label="stampadm")
    w.db.add(
        OrganizationMembership(organization_id=w.org.id, user_id=org_admin.id, role="org:admin")
    )
    w.db.commit()
    w.roles[int(org_admin.id)] = (w.org.id, "org:admin")
    headers = login_headers(w.client, org_admin)

    unstamped = _prompt(w.db, owner_a)
    stamped_here = _prompt(w.db, owner_a)
    stamped_here.organization_id = w.org.id
    stamped_elsewhere = _prompt(w.db, owner_a)
    stamped_elsewhere.organization_id = w.other_org.id
    w.db.commit()

    for control in (unstamped, stamped_here):
        served = _toggle(w.client, headers, control, shared=True)
        assert served.status_code == 200, served.text

    before = _snapshot(w.db, stamped_elsewhere)
    refused = _toggle(w.client, headers, stamped_elsewhere, shared=True)
    assert refused.status_code == 404, refused.text
    assert _snapshot(w.db, stamped_elsewhere) == before
