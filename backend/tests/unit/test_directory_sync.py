"""Directory reconciliation — the deprovisioning half of LDAP sync.

Login-time sync only ever created and promoted accounts. Nothing set
``is_active = False``, so a user deleted or disabled in Active Directory kept an
active row, and because refresh tokens rotate on every use while the session check
reads only that local flag, termination upstream did not terminate access here.

These tests pin the four properties that make an automated disabler safe to run:

* it acts on "the directory says this user is gone" and **only** that;
* it acts on **nobody** when it could not ask the directory;
* it never touches a ``super_admin`` (local-only break-glass) or a ``local`` account
  (whose identity does not live upstream at all);
* it is bounded — a per-run cap and a dry-run mode, both honoured.

No real LDAP and no real DB: the directory probe and the candidate query are the two
seams, and both are module-level functions the tests replace.
"""

# mypy: disable-error-code="arg-type"
# These tests pass structural stand-ins to signatures that declare
# Session/User/…Data. Suppressing arg-type for the file is the honest
# statement of that; the alternative is casts at every call site, or widening
# a production signature to suit a test.
from __future__ import annotations

import pytest

from app.auth.ldap_auth import DIRECTORY_ABSENT
from app.auth.ldap_auth import DIRECTORY_DISABLED
from app.auth.ldap_auth import DIRECTORY_NOT_ENTITLED
from app.auth.ldap_auth import DIRECTORY_PRESENT
from app.auth.ldap_auth import LdapConfig
from app.auth.ldap_auth import LdapDirectoryUnavailableError
from app.auth.ldap_auth import LdapProbe
from app.auth.ldap_auth import probe_ldap_user
from app.services import directory_sync_service as svc

# =============================================================================
# Fakes
# =============================================================================


class FakeUser:
    """Enough of ``models.User`` for the sweep: identity, role, auth type, flag."""

    def __init__(self, uid, email, *, role="user", auth_type="ldap", is_active=True):
        self.id = uid
        self.uuid = f"019ec90a-0000-7000-8000-0000000000{uid:02d}"
        self.email = email
        self.full_name = email
        self.role = role
        self.auth_type = auth_type
        self.ldap_uid = email.split("@")[0]
        self.is_active = is_active


class _EmptyQuery:
    """Every ``query(...).filter(...).all()`` in the sweep resolves to nothing."""

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return []


class FakeSession:
    """A Session stand-in.

    The sweep commits through it, and since v376 also reads the account's existing
    group memberships (there are none here, which is the point — a deployment with
    no ``group_mapping`` rows must reconcile to no changes at all).
    """

    def __init__(self):
        self.commits = 0
        self.rollbacks = 0
        self.added: list = []
        self.deleted: list = []

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def query(self, *args, **kwargs):
        return _EmptyQuery()

    def add(self, obj):
        self.added.append(obj)

    def delete(self, obj):
        self.deleted.append(obj)


class RecordingAudit:
    def __init__(self):
        self.events = []

    def log(self, **kwargs):
        self.events.append(kwargs)


@pytest.fixture
def db():
    return FakeSession()


@pytest.fixture
def revocations(monkeypatch):
    """Replace session revocation with a recorder; returns the list of (user, reason)."""
    calls: list[tuple[str, str]] = []

    def _revoke(_db, user, *, reason):
        calls.append((str(user.email), reason))
        return 3

    monkeypatch.setattr(svc, "revoke_all_sessions", _revoke)
    return calls


@pytest.fixture
def audit(monkeypatch):
    recorder = RecordingAudit()
    monkeypatch.setattr(svc, "audit_logger", recorder)
    return recorder


def wire(monkeypatch, users, statuses):
    """Point the sweep at *users* and make the directory answer *statuses*.

    ``statuses`` maps email -> directory status (or a ready-made ``LdapProbe``).
    A value that is an exception class or instance is raised instead, standing in
    for an unreachable directory.
    """
    monkeypatch.setattr(svc, "candidate_users", lambda _db: list(users))

    def _probe(_db, candidates):
        for user in candidates:
            answer = statuses.get(str(user.email), DIRECTORY_PRESENT)
            if isinstance(answer, BaseException):
                raise answer
            yield user, answer if isinstance(answer, LdapProbe) else LdapProbe(answer)

    monkeypatch.setattr(svc, "probe_users", _probe)


ENFORCE = svc.SweepConfig(dry_run=False, max_disables=10)


# =============================================================================
# The account is gone → disable it AND revoke its sessions
# =============================================================================


class TestDeprovisioning:
    def test_absent_user_is_disabled_and_sessions_revoked(
        self, db, monkeypatch, revocations, audit
    ):
        gone = FakeUser(1, "gone@example.com")
        stays = FakeUser(2, "stays@example.com")
        wire(monkeypatch, [gone, stays], {"gone@example.com": DIRECTORY_ABSENT})

        result = svc.sweep_ldap(db, ENFORCE)

        assert gone.is_active is False
        assert stays.is_active is True
        assert result["disabled"] == 1
        assert result["status"] == "ok"
        # Disabling without revoking is the actual hole: the refresh token keeps
        # rotating regardless of the local flag.
        assert revocations == [("gone@example.com", "directory_sync:absent_from_directory")]
        assert audit.events[0]["details"]["reason"] == "absent_from_directory"

    @pytest.mark.parametrize(
        ("status", "reason"),
        [
            (DIRECTORY_ABSENT, "absent_from_directory"),
            (DIRECTORY_DISABLED, "disabled_in_directory"),
            (DIRECTORY_NOT_ENTITLED, "no_longer_in_required_groups"),
        ],
    )
    def test_every_actionable_status_records_its_own_reason(
        self, db, monkeypatch, revocations, audit, status, reason
    ):
        user = FakeUser(1, "u@example.com")
        wire(monkeypatch, [user], {"u@example.com": status})

        result = svc.sweep_ldap(db, ENFORCE)

        assert user.is_active is False
        assert result["actions"][0]["reason"] == reason

    def test_present_user_is_left_alone(self, db, monkeypatch, revocations, audit):
        user = FakeUser(1, "here@example.com")
        wire(monkeypatch, [user], {"here@example.com": DIRECTORY_PRESENT})

        result = svc.sweep_ldap(db, ENFORCE)

        assert user.is_active is True
        assert result["disabled"] == 0
        assert revocations == []


# =============================================================================
# Fail closed on ambiguity, not on error
# =============================================================================


class TestDirectoryUnavailable:
    def test_unreachable_directory_disables_nobody(self, db, monkeypatch, revocations, audit):
        """A minute of LDAP downtime must not deprovision the whole deployment."""
        users = [FakeUser(i, f"u{i}@example.com") for i in range(1, 4)]
        wire(monkeypatch, users, {"u1@example.com": LdapDirectoryUnavailableError("bind failed")})

        result = svc.sweep_ldap(db, ENFORCE)

        assert result["status"] == "directory_unavailable"
        assert result["disabled"] == 0
        assert all(u.is_active for u in users)
        assert revocations == []
        assert audit.events == []
        assert "bind failed" in result["error"]

    def test_failure_midway_keeps_prior_findings_and_stops(
        self, db, monkeypatch, revocations, audit
    ):
        """An answered "absent" stands; everything after the failure is untouched."""
        answered = FakeUser(1, "a@example.com")
        broke = FakeUser(2, "b@example.com")
        never = FakeUser(3, "c@example.com")
        wire(
            monkeypatch,
            [answered, broke, never],
            {
                "a@example.com": DIRECTORY_ABSENT,
                "b@example.com": LdapDirectoryUnavailableError("search failed"),
            },
        )

        result = svc.sweep_ldap(db, ENFORCE)

        assert answered.is_active is False
        assert broke.is_active is True
        assert never.is_active is True
        assert result["status"] == "directory_unavailable"
        assert result["disabled"] == 1


# =============================================================================
# Accounts the sweep may never touch
# =============================================================================


class TestProtectedAccounts:
    def test_super_admin_and_local_accounts_are_never_disabled(
        self, db, monkeypatch, revocations, audit
    ):
        """Both are protected at the point of action, not only by the candidate query."""
        breakglass = FakeUser(1, "root@example.com", role="super_admin")
        local = FakeUser(2, "local@example.com", auth_type="local")
        ordinary = FakeUser(3, "ldap@example.com")
        wire(
            monkeypatch,
            [breakglass, local, ordinary],
            dict.fromkeys(
                ["root@example.com", "local@example.com", "ldap@example.com"], DIRECTORY_ABSENT
            ),
        )

        result = svc.sweep_ldap(db, ENFORCE)

        assert breakglass.is_active is True
        assert local.is_active is True
        assert ordinary.is_active is False
        assert result["disabled"] == 1
        assert revocations == [("ldap@example.com", "directory_sync:absent_from_directory")]

    def test_is_protected_predicate(self):
        assert svc.is_protected(FakeUser(1, "a@x.com", role="super_admin")) is True
        assert svc.is_protected(FakeUser(2, "b@x.com", auth_type="local")) is True
        assert svc.is_protected(FakeUser(3, "c@x.com", auth_type="keycloak")) is True
        assert svc.is_protected(FakeUser(4, "d@x.com", role="admin")) is False
        assert svc.is_protected(FakeUser(5, "e@x.com")) is False


# =============================================================================
# Bounded blast radius
# =============================================================================


class TestPerRunCap:
    def test_cap_stops_the_pass_and_leaves_the_rest_active(
        self, db, monkeypatch, revocations, audit
    ):
        """A misconfigured search_base looks exactly like mass offboarding."""
        users = [FakeUser(i, f"u{i}@example.com") for i in range(1, 6)]
        wire(monkeypatch, users, dict.fromkeys([u.email for u in users], DIRECTORY_ABSENT))

        result = svc.sweep_ldap(db, svc.SweepConfig(dry_run=False, max_disables=2))

        assert result["disabled"] == 2
        assert result["capped"] is True
        assert [u.is_active for u in users] == [False, False, True, True, True]
        assert len(revocations) == 2

    def test_zero_cap_disables_nothing(self, db, monkeypatch, revocations, audit):
        users = [FakeUser(1, "u1@example.com")]
        wire(monkeypatch, users, {"u1@example.com": DIRECTORY_ABSENT})

        result = svc.sweep_ldap(db, svc.SweepConfig(dry_run=False, max_disables=0))

        assert result["disabled"] == 0
        assert result["capped"] is True
        assert users[0].is_active is True


class TestDryRun:
    def test_dry_run_reports_but_changes_nothing(self, db, monkeypatch, revocations, audit):
        users = [FakeUser(i, f"u{i}@example.com") for i in range(1, 4)]
        wire(
            monkeypatch,
            users,
            {"u1@example.com": DIRECTORY_ABSENT, "u3@example.com": DIRECTORY_DISABLED},
        )

        result = svc.sweep_ldap(db, svc.SweepConfig(dry_run=True, max_disables=10))

        assert all(u.is_active for u in users)
        assert revocations == []
        assert audit.events == []
        assert db.commits == 0
        assert result["disabled"] == 0
        assert result["would_disable"] == 2
        assert [a["email"] for a in result["actions"]] == ["u1@example.com", "u3@example.com"]
        assert all(a["applied"] is False for a in result["actions"])

    def test_disabled_feature_runs_nothing(self, monkeypatch):
        """Default-off: the sweep must not even look at the directory."""
        monkeypatch.setattr(svc, "get_settings", lambda _db: {"enabled": False})
        monkeypatch.setattr(svc, "candidate_users", lambda _db: pytest.fail("must not query users"))

        assert svc.run_scheduled_sweep(db=FakeSession()) == {"status": "disabled"}


# =============================================================================
# The LDAP probe itself (fake ldap3 entries — no server)
# =============================================================================


class FakeAttr:
    def __init__(self, value):
        self.value = value


class FakeEntry:
    """Minimal ldap3 ``Entry``: membership test, item access, ``entry_dn``."""

    def __init__(self, attrs):
        self._attrs = attrs
        self.entry_dn = "CN=Test,OU=Users,DC=example,DC=com"

    def __contains__(self, key):
        return key in self._attrs

    def __getitem__(self, key):
        return FakeAttr(self._attrs[key])


CFG = LdapConfig(enabled=True, server="ldap.example.com", search_base="DC=example,DC=com")


class TestProbeLdapUser:
    def test_missing_entry_is_absent(self, monkeypatch):
        monkeypatch.setattr("app.auth.ldap_auth._search_ldap_user", lambda *a, **k: None)
        assert probe_ldap_user(CFG, object(), "gone").status == DIRECTORY_ABSENT

    def test_ad_account_disable_bit_is_detected(self, monkeypatch):
        # 0x202 = NORMAL_ACCOUNT | ACCOUNTDISABLE — the usual offboarded-AD-user value.
        entry = FakeEntry({"userAccountControl": "514"})
        monkeypatch.setattr("app.auth.ldap_auth._search_ldap_user", lambda *a, **k: entry)
        assert probe_ldap_user(CFG, object(), "u").status == DIRECTORY_DISABLED

    def test_enabled_account_is_present(self, monkeypatch):
        entry = FakeEntry({"userAccountControl": "512"})
        monkeypatch.setattr("app.auth.ldap_auth._search_ldap_user", lambda *a, **k: entry)
        monkeypatch.setattr("app.auth.ldap_auth._check_group_access", lambda *a, **k: True)
        assert probe_ldap_user(CFG, object(), "u").status == DIRECTORY_PRESENT

    def test_server_without_useraccountcontrol_is_not_treated_as_disabled(self, monkeypatch):
        """A missing answer is not evidence. OpenLDAP/LLDAP never send this attribute."""
        monkeypatch.setattr("app.auth.ldap_auth._search_ldap_user", lambda *a, **k: FakeEntry({}))
        monkeypatch.setattr("app.auth.ldap_auth._check_group_access", lambda *a, **k: True)
        assert probe_ldap_user(CFG, object(), "u").status == DIRECTORY_PRESENT

    def test_losing_required_group_membership_is_not_entitled(self, monkeypatch):
        monkeypatch.setattr("app.auth.ldap_auth._search_ldap_user", lambda *a, **k: FakeEntry({}))
        monkeypatch.setattr("app.auth.ldap_auth._check_group_access", lambda *a, **k: False)
        assert probe_ldap_user(CFG, object(), "u").status == DIRECTORY_NOT_ENTITLED

    def test_search_failure_raises_unavailable_rather_than_absent(self, monkeypatch):
        """The single most dangerous confusion in this feature."""
        from ldap3.core.exceptions import LDAPException

        def _boom(*a, **k):
            raise LDAPException("connection reset")

        monkeypatch.setattr("app.auth.ldap_auth._search_ldap_user", _boom)
        with pytest.raises(LdapDirectoryUnavailableError):
            probe_ldap_user(CFG, object(), "u")
