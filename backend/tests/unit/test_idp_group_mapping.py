"""IdP group mapping — resolution, reconciliation semantics, and the super_admin cap.

Before ``v376`` both directory paths built the caller's full group list and then
discarded it: ``LdapUserData.groups`` and ``OIDCUserData.roles`` existed, and
only ``is_admin`` survived. ``UserGroup``/``UserGroupMember`` had **no auth code
referencing them at all**, so a directory group could not become a sharing group.

These tests pin the properties that make automatic membership safe:

* a claim that matches nothing changes nothing;
* an in-app group is joined only while the directory still asserts it, and left
  when it stops — but **only** for rows a directory pass created;
* a hand-added membership is never removed, never duplicated, and never converted
  into a directory-owned one;
* no mapping can produce ``super_admin``, and no directory signal can demote one.

No real database: the two seams are "read the mappings" and "read this user's
memberships", and both are single queries the fake session below answers.
"""

# mypy: disable-error-code="arg-type"
# The service signatures declare Session/User; every test here passes a
# structural stand-in on purpose. Suppressing arg-type for this file is the
# honest statement of that — the alternative is ~30 casts that would bury the
# assertions, or widening a production signature to suit a test.
from __future__ import annotations

import pytest

from app.auth.roles import ROLE_ADMIN
from app.auth.roles import ROLE_SUPER_ADMIN
from app.auth.roles import ROLE_USER
from app.models.group import MAPPING_SOURCE_LDAP
from app.models.group import MAPPING_SOURCE_OIDC
from app.models.group import MEMBERSHIP_SOURCE_MANUAL
from app.models.group import GroupMapping
from app.models.group import UserGroupMember
from app.services import idp_group_mapping_service as svc

LEGAL_DN = "CN=Legal-Team,OU=Groups,DC=corp,DC=example"
ENG_DN = "CN=Engineering,OU=Groups,DC=corp,DC=example"


# =============================================================================
# Fakes
# =============================================================================
class FakeMapping:
    """Enough of ``GroupMapping`` for the resolver."""

    def __init__(self, source, claim_value, user_group_id=None, grants_role=None):
        self.id = id(self)
        self.source = source
        self.claim_value = claim_value
        self.user_group_id = user_group_id
        self.grants_role = grants_role


class FakeMember:
    """Enough of ``UserGroupMember`` for the reconciler."""

    def __init__(self, group_id, source=MEMBERSHIP_SOURCE_MANUAL):
        self.group_id = group_id
        self.user_id = 1
        self.source = source
        self.role = "member"


class FakeUser:
    def __init__(self, role=ROLE_USER):
        self.id = 1
        self.uuid = "019ec90a-0000-7000-8000-000000000001"
        self.email = "user@corp.example"
        self.role = role
        self.is_superuser = role == ROLE_SUPER_ADMIN


class _Result:
    """Applies simple ``Column == value`` criteria so source scoping is real here.

    ``resolve_grants`` filters by ``source`` in SQL, and "an OIDC mapping must not
    match an LDAP claim" is a property worth pinning rather than assuming.
    """

    def __init__(self, rows):
        self._rows = rows

    def filter(self, *criteria):
        rows = self._rows
        for criterion in criteria:
            attr = getattr(getattr(criterion, "left", None), "key", None)
            value = getattr(getattr(criterion, "right", None), "value", None)
            if attr is None:
                continue
            rows = [r for r in rows if getattr(r, attr, None) == value]
        return _Result(rows)

    def all(self):
        return list(self._rows)


class FakeSession:
    """Answers the resolver's mapping query and the reconciler's membership query."""

    def __init__(self, mappings=(), members=()):
        self.mappings = list(mappings)
        self.members = list(members)
        self.added: list = []
        self.deleted: list = []
        self.commits = 0

    def query(self, model, *args):
        if model is GroupMapping:
            return _Result(self.mappings)
        if model is UserGroupMember:
            return _Result(self.members)
        return _Result([])

    def add(self, obj):
        self.added.append(obj)

    def delete(self, obj):
        self.deleted.append(obj)

    def commit(self):
        self.commits += 1


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch):
    """Keep session revocation and the OpenSearch-backed audit log out of unit tests."""
    monkeypatch.setattr(svc, "revoke_all_sessions", lambda _db, _u, *, reason: 2)

    class _Audit:
        def __init__(self):
            self.events = []

        def log(self, **kwargs):
            self.events.append(kwargs)

    recorder = _Audit()
    monkeypatch.setattr(svc, "audit_logger", recorder)
    return recorder


# =============================================================================
# The super_admin cap — the load-bearing rule
# =============================================================================
class TestSuperAdminIsUnreachableFromAnIdP:
    def test_assert_grantable_role_refuses_super_admin(self):
        with pytest.raises(svc.RoleNotGrantableError):
            svc.assert_grantable_role(ROLE_SUPER_ADMIN)

    def test_assert_grantable_role_refuses_an_unknown_value(self):
        with pytest.raises(svc.RoleNotGrantableError):
            svc.assert_grantable_role("platform_owner")

    def test_admin_and_user_are_grantable_and_empty_is_none(self):
        assert svc.assert_grantable_role(ROLE_ADMIN) == ROLE_ADMIN
        assert svc.assert_grantable_role(ROLE_USER) == ROLE_USER
        assert svc.assert_grantable_role(None) is None
        assert svc.assert_grantable_role("") is None

    def test_super_admin_is_not_in_the_grantable_set(self):
        """A regression guard: widening this tuple would be an escalation bug."""
        assert ROLE_SUPER_ADMIN not in svc.GRANTABLE_ROLES

    def test_a_super_admin_is_never_demoted_by_the_directory(self):
        """The break-glass account survives a directory that stopped asserting admin."""
        db = FakeSession()
        user = FakeUser(role=ROLE_SUPER_ADMIN)

        result = svc.reconcile_user(db, user, MAPPING_SOURCE_LDAP, [], legacy_admin=False)

        assert user.role == ROLE_SUPER_ADMIN
        assert result.role_after is None
        assert result.role_changed is False

    def test_a_super_admin_is_not_touched_even_when_a_mapping_grants_admin(self):
        db = FakeSession([FakeMapping(MAPPING_SOURCE_LDAP, LEGAL_DN, grants_role=ROLE_ADMIN)])
        user = FakeUser(role=ROLE_SUPER_ADMIN)

        svc.reconcile_user(db, user, MAPPING_SOURCE_LDAP, [LEGAL_DN])

        assert user.role == ROLE_SUPER_ADMIN
        assert user.is_superuser is True


# =============================================================================
# Resolution
# =============================================================================
class TestResolveGrants:
    def test_no_mappings_resolves_to_nothing(self):
        grants = svc.resolve_grants(FakeSession(), MAPPING_SOURCE_LDAP, [LEGAL_DN])
        assert grants.group_ids == frozenset()
        assert grants.role is None
        assert grants.matched_claims == ()

    def test_a_matching_claim_yields_its_group(self):
        db = FakeSession([FakeMapping(MAPPING_SOURCE_LDAP, LEGAL_DN, user_group_id=7)])
        grants = svc.resolve_grants(db, MAPPING_SOURCE_LDAP, [LEGAL_DN, ENG_DN])
        assert grants.group_ids == frozenset({7})
        assert grants.matched_claims == (LEGAL_DN,)

    def test_ldap_claims_match_case_insensitively(self):
        """AD returns DNs in whatever case it likes; the existing member check folds too."""
        db = FakeSession([FakeMapping(MAPPING_SOURCE_LDAP, LEGAL_DN.upper(), user_group_id=7)])
        grants = svc.resolve_grants(db, MAPPING_SOURCE_LDAP, [LEGAL_DN.lower()])
        assert grants.group_ids == frozenset({7})

    def test_oidc_claims_match_exactly(self):
        """OIDC role strings are opaque identifiers — folding them would merge roles."""
        db = FakeSession([FakeMapping(MAPPING_SOURCE_OIDC, "Legal", user_group_id=7)])
        assert svc.resolve_grants(db, MAPPING_SOURCE_OIDC, ["legal"]).group_ids == frozenset()
        assert svc.resolve_grants(db, MAPPING_SOURCE_OIDC, ["Legal"]).group_ids == frozenset({7})

    def test_sources_are_separate_namespaces(self):
        db = FakeSession([FakeMapping(MAPPING_SOURCE_OIDC, "legal", user_group_id=7)])
        assert svc.resolve_grants(db, MAPPING_SOURCE_LDAP, ["legal"]).group_ids == frozenset()

    def test_admin_beats_user_across_several_matches(self):
        db = FakeSession(
            [
                FakeMapping(MAPPING_SOURCE_OIDC, "staff", user_group_id=1, grants_role=ROLE_USER),
                FakeMapping(MAPPING_SOURCE_OIDC, "ops", user_group_id=2, grants_role=ROLE_ADMIN),
            ]
        )
        grants = svc.resolve_grants(db, MAPPING_SOURCE_OIDC, ["staff", "ops"])
        assert grants.role == ROLE_ADMIN
        assert grants.grants_admin is True
        assert grants.group_ids == frozenset({1, 2})

    def test_a_malformed_claim_value_never_raises_inside_a_login(self):
        db = FakeSession([FakeMapping(MAPPING_SOURCE_OIDC, "ops", grants_role=ROLE_ADMIN)])
        assert svc.resolve_grants(db, MAPPING_SOURCE_OIDC, None).role is None
        assert svc.resolve_grants(db, MAPPING_SOURCE_OIDC, "ops").role is None

    def test_an_unknown_source_is_a_programming_error(self):
        with pytest.raises(ValueError, match="Unknown directory source"):
            svc.resolve_grants(FakeSession(), "saml", ["x"])


# =============================================================================
# Membership reconciliation
# =============================================================================
class TestMembershipReconciliation:
    def test_a_matched_mapping_adds_a_directory_owned_membership(self):
        db = FakeSession([FakeMapping(MAPPING_SOURCE_LDAP, LEGAL_DN, user_group_id=7)])
        result = svc.reconcile_user(db, FakeUser(), MAPPING_SOURCE_LDAP, [LEGAL_DN])

        assert result.groups_added == [7]
        assert len(db.added) == 1
        assert db.added[0].source == MAPPING_SOURCE_LDAP
        assert db.added[0].role == "member"

    def test_losing_the_directory_group_removes_the_membership(self):
        db = FakeSession(members=[FakeMember(7, source=MAPPING_SOURCE_LDAP)])
        result = svc.reconcile_user(db, FakeUser(), MAPPING_SOURCE_LDAP, [])

        assert result.groups_removed == [7]
        assert len(db.deleted) == 1

    def test_deleting_the_mapping_removes_the_membership_on_the_next_pass(self):
        """No mapping rows at all: the previously-derived membership resolves to nothing."""
        db = FakeSession(mappings=[], members=[FakeMember(7, source=MAPPING_SOURCE_LDAP)])
        assert svc.reconcile_user(
            db, FakeUser(), MAPPING_SOURCE_LDAP, [LEGAL_DN]
        ).groups_removed == [7]

    def test_a_hand_added_membership_is_never_removed(self):
        """The whole reason ``user_group_member.source`` exists."""
        db = FakeSession(members=[FakeMember(7, source=MEMBERSHIP_SOURCE_MANUAL)])
        result = svc.reconcile_user(db, FakeUser(), MAPPING_SOURCE_LDAP, [])

        assert result.groups_removed == []
        assert db.deleted == []

    def test_a_mapping_onto_a_hand_added_group_leaves_it_manual(self):
        """No duplicate row, and no silent conversion that a later pass could revoke."""
        db = FakeSession(
            mappings=[FakeMapping(MAPPING_SOURCE_LDAP, LEGAL_DN, user_group_id=7)],
            members=[FakeMember(7, source=MEMBERSHIP_SOURCE_MANUAL)],
        )
        result = svc.reconcile_user(db, FakeUser(), MAPPING_SOURCE_LDAP, [LEGAL_DN])

        assert result.groups_added == []
        assert db.added == []
        assert db.members[0].source == MEMBERSHIP_SOURCE_MANUAL

    def test_a_steady_state_pass_writes_nothing(self):
        db = FakeSession(
            mappings=[FakeMapping(MAPPING_SOURCE_LDAP, LEGAL_DN, user_group_id=7)],
            members=[FakeMember(7, source=MAPPING_SOURCE_LDAP)],
        )
        result = svc.reconcile_user(db, FakeUser(), MAPPING_SOURCE_LDAP, [LEGAL_DN])

        assert result.changed is False
        assert db.commits == 0

    def test_dry_run_reports_the_plan_and_writes_nothing(self):
        db = FakeSession(
            mappings=[FakeMapping(MAPPING_SOURCE_LDAP, LEGAL_DN, user_group_id=7)],
            members=[FakeMember(9, source=MAPPING_SOURCE_LDAP)],
        )
        result = svc.reconcile_user(db, FakeUser(), MAPPING_SOURCE_LDAP, [LEGAL_DN], dry_run=True)

        assert result.groups_added == [7]
        assert result.groups_removed == [9]
        assert result.applied is False
        assert db.added == [] and db.deleted == [] and db.commits == 0


# =============================================================================
# Privilege
# =============================================================================
class TestPrivilege:
    def test_a_mapping_that_grants_admin_promotes(self, _no_side_effects):
        db = FakeSession([FakeMapping(MAPPING_SOURCE_OIDC, "ops", grants_role=ROLE_ADMIN)])
        user = FakeUser()

        result = svc.reconcile_user(db, user, MAPPING_SOURCE_OIDC, ["ops"])

        assert user.role == ROLE_ADMIN
        assert user.is_superuser is False
        assert result.role_before == ROLE_USER
        assert result.role_after == ROLE_ADMIN

    def test_a_privilege_change_revokes_sessions_and_is_audited(self, _no_side_effects):
        db = FakeSession([FakeMapping(MAPPING_SOURCE_OIDC, "ops", grants_role=ROLE_ADMIN)])
        result = svc.reconcile_user(db, FakeUser(), MAPPING_SOURCE_OIDC, ["ops"])

        assert result.sessions_revoked == 2
        assert len(_no_side_effects.events) == 1
        assert _no_side_effects.events[0]["details"]["new_role"] == ROLE_ADMIN

    def test_losing_the_admin_claim_demotes(self):
        db = FakeSession()
        user = FakeUser(role=ROLE_ADMIN)

        svc.reconcile_user(db, user, MAPPING_SOURCE_LDAP, [], legacy_admin=False)

        assert user.role == ROLE_USER
        assert user.is_superuser is False

    def test_the_legacy_admin_signal_still_promotes_with_no_mappings(self):
        """A deployment using ldap_admin_groups and no mappings behaves as before v376."""
        db = FakeSession()
        user = FakeUser()

        svc.reconcile_user(db, user, MAPPING_SOURCE_LDAP, [ENG_DN], legacy_admin=True)

        assert user.role == ROLE_ADMIN

    def test_the_legacy_signal_alone_keeps_an_admin_admin(self):
        """The sweep passes the same rule login uses; without it, mass demotion."""
        db = FakeSession()
        user = FakeUser(role=ROLE_ADMIN)

        result = svc.reconcile_user(db, user, MAPPING_SOURCE_LDAP, [ENG_DN], legacy_admin=True)

        assert user.role == ROLE_ADMIN
        assert result.role_changed is False

    def test_a_user_grant_does_not_demote_nor_promote(self):
        db = FakeSession([FakeMapping(MAPPING_SOURCE_OIDC, "staff", grants_role=ROLE_USER)])
        user = FakeUser()

        result = svc.reconcile_user(db, user, MAPPING_SOURCE_OIDC, ["staff"])

        assert user.role == ROLE_USER
        assert result.role_changed is False

    def test_dry_run_reports_the_role_change_without_applying_it(self):
        db = FakeSession([FakeMapping(MAPPING_SOURCE_OIDC, "ops", grants_role=ROLE_ADMIN)])
        user = FakeUser()

        result = svc.reconcile_user(db, user, MAPPING_SOURCE_OIDC, ["ops"], dry_run=True)

        assert result.role_after == ROLE_ADMIN
        assert user.role == ROLE_USER
