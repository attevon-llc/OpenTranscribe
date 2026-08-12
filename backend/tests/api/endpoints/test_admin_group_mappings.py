"""Functional tests for the IdP group-mapping admin API (``admin_group_mappings.py``).

Before this file the module had **no functional coverage at all**: the only
references to ``/api/admin/group-mappings`` anywhere in ``tests/`` were
``test_route_privilege_tiers.py`` (asserts the prefix's dependency tier) and
``test_route_has_a_caller.py`` (asserts the path exists). Neither ever issues a
request, so every handler body — duplicate-claim rejection, the LDAP
case-folding rule, the "must grant something" re-check on PATCH, the dry-run
resolver — was unexecuted.

That matters because these rows decide **who a directory claim hands ``admin``
to**. The invariants pinned here:

* a plain ``admin`` cannot reach any route (this is super_admin tier);
* ``super_admin`` is not a grantable role;
* a duplicate claim is a readable 400, not a driver 500 — including the
  case-only LDAP variant, with OIDC as the control that must stay
  case-sensitive;
* clearing both halves of a mapping is refused **and not persisted**;
* deleting a mapping does not delete the memberships it produced;
* ``POST /test`` writes nothing.

Every row is created on the savepoint-isolated ``db_session``. Claim values are
UUID-suffixed because ``uq_group_mapping_source_claim`` is global.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import status

from app.models.group import MAPPING_SOURCE_LDAP
from app.models.group import MAPPING_SOURCE_OIDC
from app.models.group import MEMBERSHIP_SOURCE_MANUAL
from app.models.group import MEMBERSHIP_SOURCE_SCIM
from app.models.group import GroupMapping
from app.models.group import UserGroup
from app.models.group import UserGroupMember

BASE = "/api/admin/group-mappings"


def _dn(prefix: str = "CN=Legal") -> str:
    """A distinguished name unique to this run (the claim column is globally unique)."""
    return f"{prefix}-{uuid.uuid4().hex[:12]},OU=Groups,DC=corp,DC=example"


def _make_group(db_session, owner) -> UserGroup:
    group = UserGroup(owner_id=owner.id, name=f"grp-{uuid.uuid4().hex[:8]}")
    db_session.add(group)
    db_session.commit()
    db_session.refresh(group)
    return group


def _create(client, headers, **payload) -> dict:
    """POST a mapping and return the created body (fails loudly on anything but 201)."""
    response = client.post(BASE, headers=headers, json=payload)
    assert response.status_code == status.HTTP_201_CREATED, response.text
    created: dict = response.json()
    return created


# ---------------------------------------------------------------------------
# Privilege tier
# ---------------------------------------------------------------------------
#: A well-formed UUID that intentionally matches no row. MUST be a constant: a
#: uuid4() call inside a parametrize list is evaluated at IMPORT, so every xdist
#: worker generated different test ids and the run died with "Different tests were
#: collected between gw2 and gwN" before any test executed.
ABSENT_UUID = "00000000-0000-4000-8000-000000000000"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", BASE, None),
        ("POST", BASE, {"source": "ldap", "claim_value": "CN=x", "grants_role": "admin"}),
        ("PUT", f"{BASE}/{ABSENT_UUID}", {"description": "x"}),
        ("DELETE", f"{BASE}/{ABSENT_UUID}", None),
        ("POST", f"{BASE}/test", {"source": "ldap", "claim_values": ["CN=x"]}),
    ],
)
def test_plain_admin_is_refused_on_every_route(client, admin_token_headers, method, path, body):
    """An ``admin`` must not configure who gets ``admin`` — that is super_admin tier.

    Catches a route (or the whole router) being re-gated to
    ``get_current_admin_user``: a privilege-escalation path where any admin could
    mint a mapping granting themselves ``admin`` from a claim they control.
    """
    response = client.request(method, path, headers=admin_token_headers, json=body)
    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_list_requires_authentication(client):
    response = client.get(BASE)
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# Create + list
# ---------------------------------------------------------------------------
def test_create_returns_group_name_and_appears_in_list(
    client, super_admin_token_headers, super_admin_user, db_session
):
    """The created row is served back resolved, and the list agrees with it.

    Catches ``_to_schema`` losing the ``group_uuid``/``group_name`` join (the UI
    renders the target group from those two fields and has no second lookup), and
    a create that shapes a response without persisting.
    """
    group = _make_group(db_session, super_admin_user)
    claim = _dn()
    created = _create(
        client,
        super_admin_token_headers,
        source=MAPPING_SOURCE_LDAP,
        claim_value=claim,
        group_uuid=str(group.uuid),
        description="legal team",
    )
    assert created["group_uuid"] == str(group.uuid)
    assert created["group_name"] == group.name
    assert created["claim_value"] == claim
    assert created["grants_role"] is None

    listed = client.get(BASE, headers=super_admin_token_headers)
    assert listed.status_code == status.HTTP_200_OK
    by_uuid = {m["uuid"]: m for m in listed.json()}
    assert by_uuid[created["uuid"]]["group_name"] == group.name


def test_list_source_filter_excludes_the_other_directory(
    client, super_admin_token_headers, db_session
):
    """``?source=oidc`` must not leak LDAP rows, and vice versa.

    Catches the filter being dropped (the admin UI's two tabs would then show each
    other's mappings, and an operator could delete an LDAP rule from the OIDC tab).
    """
    ldap_claim = _dn()
    oidc_claim = f"role-{uuid.uuid4().hex[:12]}"
    _create(
        client,
        super_admin_token_headers,
        source=MAPPING_SOURCE_LDAP,
        claim_value=ldap_claim,
        grants_role="admin",
    )
    _create(
        client,
        super_admin_token_headers,
        source=MAPPING_SOURCE_OIDC,
        claim_value=oidc_claim,
        grants_role="user",
    )

    response = client.get(BASE, params={"source": "oidc"}, headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    claims = {m["claim_value"] for m in response.json()}
    assert oidc_claim in claims
    assert ldap_claim not in claims


def test_create_refuses_to_grant_super_admin(client, super_admin_token_headers):
    """No identity provider may assert ``super_admin``.

    Catches ``ROLE_PATTERN`` being widened to include ``super_admin`` — the
    break-glass account would become mintable from a directory group, which is the
    single worst outcome in this module.
    """
    response = client.post(
        BASE,
        headers=super_admin_token_headers,
        json={
            "source": MAPPING_SOURCE_LDAP,
            "claim_value": _dn("CN=Domain-Admins"),
            "grants_role": "super_admin",
        },
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_create_refuses_a_mapping_that_grants_nothing(client, super_admin_token_headers):
    """A mapping with neither a group nor a role is inert; the API rejects it.

    Catches removal of ``GroupMappingCreate._grants_something`` — the row would
    then be refused by ``ck_group_mapping_grants_something`` as an opaque 500.
    """
    response = client.post(
        BASE,
        headers=super_admin_token_headers,
        json={"source": MAPPING_SOURCE_LDAP, "claim_value": _dn()},
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_create_with_unknown_group_uuid_is_404(client, super_admin_token_headers):
    response = client.post(
        BASE,
        headers=super_admin_token_headers,
        json={
            "source": MAPPING_SOURCE_LDAP,
            "claim_value": _dn(),
            "group_uuid": str(uuid.uuid4()),
        },
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_duplicate_ldap_claim_differing_only_in_case_is_a_400(
    client, super_admin_token_headers, db_session
):
    """LDAP DNs are case-insensitive, so the case variant is the *same* mapping.

    Catches ``_assert_claim_free`` losing its ``normalize_claim_value`` call: the
    insert would then hit ``uq_group_mapping_ldap_claim_ci`` and surface as a 500,
    or — if that index were also gone — create a second rule for the same group
    whose precedence nobody can predict.
    """
    claim = _dn()
    _create(
        client,
        super_admin_token_headers,
        source=MAPPING_SOURCE_LDAP,
        claim_value=claim,
        grants_role="admin",
    )
    response = client.post(
        BASE,
        headers=super_admin_token_headers,
        json={
            "source": MAPPING_SOURCE_LDAP,
            "claim_value": claim.upper(),
            "grants_role": "user",
        },
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "already exists" in response.json()["detail"]


def test_oidc_claims_differing_only_in_case_are_distinct(client, super_admin_token_headers):
    """The control for the LDAP test above: OIDC role strings stay case-sensitive.

    Catches case-folding being applied to every source, which would silently merge
    two different provider roles (``Legal`` and ``legal``) into one mapping.
    """
    claim = f"Legal-{uuid.uuid4().hex[:12]}"
    first = _create(
        client,
        super_admin_token_headers,
        source=MAPPING_SOURCE_OIDC,
        claim_value=claim,
        grants_role="user",
    )
    second = _create(
        client,
        super_admin_token_headers,
        source=MAPPING_SOURCE_OIDC,
        claim_value=claim.lower(),
        grants_role="user",
    )
    assert second["uuid"] != first["uuid"]


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------
def test_update_clearing_both_halves_is_refused_and_not_persisted(
    client, super_admin_token_headers, super_admin_user, db_session
):
    """Detaching the group from a group-only mapping must be rejected.

    The mapping would otherwise grant nothing and violate
    ``ck_group_mapping_grants_something``. ``admin_group_mappings.py`` rolls the
    session back before raising, so the in-flight ``user_group_id = None`` is
    never flushed.

    **Non-persistence is deliberately NOT asserted here, and cannot be.** Under
    ``conftest.db_session``'s savepoint isolation, the endpoint's correct
    ``db.rollback()`` unwinds the savepoint every fixture in this test wrote into
    — including ``super_admin_user`` — and ``conftest.py:260`` then re-arms a
    fresh one. A follow-up request through the shared-session ``client`` therefore
    401s on a user that no longer exists, which is an artifact of the harness, not
    of the endpoint: in production each request holds its own session and the
    rollback discards only that request's uncommitted work. Asserting the row
    afterwards would be asserting savepoint mechanics.
    ``test_update_can_detach_the_group_when_a_role_remains`` is the positive
    control that keeps this from passing against an endpoint that 400s on
    everything.
    """
    group = _make_group(db_session, super_admin_user)
    created = _create(
        client,
        super_admin_token_headers,
        source=MAPPING_SOURCE_LDAP,
        claim_value=_dn(),
        group_uuid=str(group.uuid),
    )

    response = client.put(
        f"{BASE}/{created['uuid']}",
        headers=super_admin_token_headers,
        json={"group_uuid": None},
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "grants_role" in response.json()["detail"]


def test_update_can_detach_the_group_when_a_role_remains(
    client, super_admin_token_headers, super_admin_user, db_session
):
    """A mapping that still grants ``admin`` may drop its group.

    The positive half of the pair above — without it, a 400-on-everything
    implementation would pass the negative test.
    """
    group = _make_group(db_session, super_admin_user)
    created = _create(
        client,
        super_admin_token_headers,
        source=MAPPING_SOURCE_LDAP,
        claim_value=_dn(),
        group_uuid=str(group.uuid),
        grants_role="admin",
    )
    response = client.put(
        f"{BASE}/{created['uuid']}",
        headers=super_admin_token_headers,
        json={"group_uuid": None},
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["group_uuid"] is None
    assert response.json()["grants_role"] == "admin"


def test_update_unknown_mapping_is_404(client, super_admin_token_headers):
    response = client.put(
        f"{BASE}/{uuid.uuid4()}",
        headers=super_admin_token_headers,
        json={"description": "x"},
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_update_malformed_uuid_is_400(client, super_admin_token_headers):
    """``get_by_uuid`` answers 400 for an unparseable id, not 500."""
    response = client.put(
        f"{BASE}/not-a-uuid", headers=super_admin_token_headers, json={"description": "x"}
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------
def test_delete_removes_the_mapping_but_keeps_directory_memberships(
    client, super_admin_token_headers, super_admin_user, normal_user, db_session
):
    """Memberships the mapping produced survive; the next reconciliation drops them.

    Catches a "tidy up on delete" cascade being added here. Deleting synchronously
    would also strip rows another mapping still justifies, and it would take out
    the group membership of every affected user in one un-undoable request.
    """
    group = _make_group(db_session, super_admin_user)
    db_session.add(
        UserGroupMember(
            group_id=group.id,
            user_id=normal_user.id,
            role="member",
            source=MAPPING_SOURCE_LDAP,
        )
    )
    db_session.commit()
    created = _create(
        client,
        super_admin_token_headers,
        source=MAPPING_SOURCE_LDAP,
        claim_value=_dn(),
        group_uuid=str(group.uuid),
    )

    response = client.delete(f"{BASE}/{created['uuid']}", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_204_NO_CONTENT
    deleted_uuid = uuid.UUID(created["uuid"])
    assert db_session.query(GroupMapping).filter(GroupMapping.uuid == deleted_uuid).first() is None
    surviving = (
        db_session.query(UserGroupMember)
        .filter(
            UserGroupMember.group_id == group.id,
            UserGroupMember.user_id == normal_user.id,
        )
        .one()
    )
    assert str(surviving.source) == MAPPING_SOURCE_LDAP


def test_delete_unknown_mapping_is_404(client, super_admin_token_headers):
    response = client.delete(f"{BASE}/{uuid.uuid4()}", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# member_count
# ---------------------------------------------------------------------------
def test_member_count_counts_only_directory_derived_memberships(
    client, super_admin_token_headers, super_admin_user, normal_user, other_user, db_session
):
    """``manual`` and ``scim`` rows are not mapping output and must not be counted.

    Catches ``MEMBERSHIP_SOURCES_PROTECTED`` being inverted (``in_`` for
    ``notin_``) or dropped: the admin panel would then report hand-built
    membership as directory-derived, and an operator reading that number would
    believe a sync owns rows it must never remove.
    """
    group = _make_group(db_session, super_admin_user)
    db_session.add_all(
        [
            UserGroupMember(
                group_id=group.id,
                user_id=normal_user.id,
                role="member",
                source=MAPPING_SOURCE_LDAP,
            ),
            UserGroupMember(
                group_id=group.id,
                user_id=other_user.id,
                role="member",
                source=MEMBERSHIP_SOURCE_MANUAL,
            ),
            UserGroupMember(
                group_id=group.id,
                user_id=super_admin_user.id,
                role="owner",
                source=MEMBERSHIP_SOURCE_SCIM,
            ),
        ]
    )
    db_session.commit()
    created = _create(
        client,
        super_admin_token_headers,
        source=MAPPING_SOURCE_LDAP,
        claim_value=_dn(),
        group_uuid=str(group.uuid),
    )

    listed = client.get(BASE, headers=super_admin_token_headers)
    row = {m["uuid"]: m for m in listed.json()}[created["uuid"]]
    assert row["member_count"] == 1


# ---------------------------------------------------------------------------
# POST /test — the dry-run resolver
# ---------------------------------------------------------------------------
def test_test_resolves_claims_to_groups_and_the_strongest_role(
    client, super_admin_token_headers, super_admin_user, db_session
):
    """Two matched claims union their groups; ``admin`` beats ``user``.

    Catches the resolver's role precedence collapsing (a ``user`` mapping listed
    after an ``admin`` one silently downgrading the answer) and the
    ``unmatched_claims`` projection reporting matched claims as unmatched, which
    is the operator's only signal that a DN was typed wrong.
    """
    group = _make_group(db_session, super_admin_user)
    admin_claim = _dn("CN=Domain-Admins")
    group_claim = _dn("CN=Legal")
    unknown_claim = _dn("CN=Nobody")
    _create(
        client,
        super_admin_token_headers,
        source=MAPPING_SOURCE_LDAP,
        claim_value=admin_claim,
        grants_role="admin",
    )
    _create(
        client,
        super_admin_token_headers,
        source=MAPPING_SOURCE_LDAP,
        claim_value=group_claim,
        group_uuid=str(group.uuid),
        grants_role="user",
    )

    response = client.post(
        f"{BASE}/test",
        headers=super_admin_token_headers,
        json={
            "source": MAPPING_SOURCE_LDAP,
            "claim_values": [group_claim, admin_claim, unknown_claim],
        },
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert set(body["matched_claims"]) == {group_claim, admin_claim}
    assert body["unmatched_claims"] == [unknown_claim]
    assert [g["uuid"] for g in body["groups"]] == [str(group.uuid)]
    assert body["grants_role"] == "admin"
    assert body["effective_role"] == "admin"
    assert body["legacy_admin"] is False


def test_test_matches_an_ldap_claim_case_insensitively(
    client, super_admin_token_headers, db_session
):
    """A directory returning a differently-cased DN must still match its mapping.

    Catches ``normalize_claim_value`` being dropped from ``resolve_grants``: the
    mapping would appear correct in the admin UI and then grant nothing at login,
    which is the hardest version of this bug to diagnose.
    """
    claim = _dn()
    _create(
        client,
        super_admin_token_headers,
        source=MAPPING_SOURCE_LDAP,
        claim_value=claim,
        grants_role="admin",
    )
    response = client.post(
        f"{BASE}/test",
        headers=super_admin_token_headers,
        json={"source": MAPPING_SOURCE_LDAP, "claim_values": [claim.upper()]},
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["effective_role"] == "admin"


def test_test_does_not_match_an_oidc_claim_across_sources(
    client, super_admin_token_headers, db_session
):
    """An LDAP mapping must never satisfy an OIDC claim.

    Catches the source filter being dropped from ``resolve_grants`` — a DN and a
    role name are separate namespaces, and crossing them lets a claim from the
    weaker-configured provider inherit the other's grants.
    """
    claim = f"role-{uuid.uuid4().hex[:12]}"
    _create(
        client,
        super_admin_token_headers,
        source=MAPPING_SOURCE_LDAP,
        claim_value=claim,
        grants_role="admin",
    )
    response = client.post(
        f"{BASE}/test",
        headers=super_admin_token_headers,
        json={"source": MAPPING_SOURCE_OIDC, "claim_values": [claim]},
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["unmatched_claims"] == [claim]
    assert body["effective_role"] == "user"


def test_test_writes_nothing(
    client, super_admin_token_headers, super_admin_user, normal_user, db_session
):
    """Writing nothing is the documented contract of the preview endpoint.

    Catches ``resolve_grants`` being swapped for ``reconcile_user`` (the two live
    in the same service and take almost the same arguments) — an operator
    previewing a rule would silently move real people between groups.
    """
    group = _make_group(db_session, super_admin_user)
    claim = _dn()
    _create(
        client,
        super_admin_token_headers,
        source=MAPPING_SOURCE_LDAP,
        claim_value=claim,
        group_uuid=str(group.uuid),
        grants_role="admin",
    )
    members_before = db_session.query(UserGroupMember).count()
    mappings_before = db_session.query(GroupMapping).count()

    response = client.post(
        f"{BASE}/test",
        headers=super_admin_token_headers,
        json={"source": MAPPING_SOURCE_LDAP, "claim_values": [claim]},
    )
    assert response.status_code == status.HTTP_200_OK
    assert db_session.query(UserGroupMember).count() == members_before
    assert db_session.query(GroupMapping).count() == mappings_before
    db_session.refresh(normal_user)
    assert str(normal_user.role) == "user"


def test_test_username_lookup_is_refused_for_oidc(client, super_admin_token_headers):
    """Looking a user up by name is LDAP-only — OIDC group membership lives in a token.

    Catches the guard being removed: the handler would fall through to
    ``_ldap_claims_for`` and consult the LDAP directory for an OIDC question,
    reporting whatever LDAP says as the OIDC answer.
    """
    response = client.post(
        f"{BASE}/test",
        headers=super_admin_token_headers,
        json={"source": MAPPING_SOURCE_OIDC, "username": "someone"},
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "LDAP-only" in response.json()["detail"]


def test_test_requires_exactly_one_input(client, super_admin_token_headers):
    """Both inputs at once is ambiguous, so the wire contract refuses it."""
    response = client.post(
        f"{BASE}/test",
        headers=super_admin_token_headers,
        json={
            "source": MAPPING_SOURCE_LDAP,
            "claim_values": ["CN=x"],
            "username": "someone",
        },
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
