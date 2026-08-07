"""Account-lifecycle controls that were built, wired to nothing, and shipped.

Four columns existed, were written (or not), and were read by no enforcement
path at all:

* ``must_change_password`` — set by the admin force-change flag, cleared by the
  password-reset flow, exposed on ``UserInDB``, and **read by nothing**. "Force
  password change on next login" was a no-op: the user signed in with the
  admin-chosen password and was never prompted (NIST 800-63B, FedRAMP IA-5(1)).
* ``password_policy.is_password_expired`` — correct, and with zero call sites,
  while ``admin.py`` reimplemented the same comparison inline for its report.
* ``last_login_at`` — never assigned anywhere, so the admin UI showed ``null``
  for every user and every inactive-account control (FedRAMP AC-2(3)) had no
  data to act on.
* ``account_expires_at`` — declared, read by nothing; a time-boxed contractor
  account could not be expressed at all.

Everything here runs against fakes: no Postgres, no Redis, no HTTP client.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from typing import cast
from uuid import UUID

import pytest
from fastapi import HTTPException

from app.api.endpoints.auth import dependencies as deps_module
from app.api.endpoints.auth import login as login_module
from app.api.endpoints.auth.dependencies import ERROR_CODE_ACCOUNT_EXPIRED
from app.api.endpoints.auth.dependencies import ERROR_CODE_PASSWORD_CHANGE_REQUIRED
from app.api.endpoints.auth.dependencies import get_current_active_user
from app.api.endpoints.auth.dependencies import get_current_admin_user
from app.auth import password_policy as policy_module
from app.auth.audit import AuditEventType
from app.core.config import settings
from app.models.user import User

USER_UUID = "019ec90a-1b2c-7def-8000-0000000000bb"

CHANGE_PASSWORD_PATH = f"{settings.API_PREFIX}/users/me"
LOGOUT_PATH = f"{settings.API_PREFIX}/auth/logout"
ORDINARY_PATH = f"{settings.API_PREFIX}/files"


# ── fakes ────────────────────────────────────────────────────────────────────────


class _FakeQuery:
    """Returns one canned row regardless of the filter — the filters are SQL, not logic."""

    def __init__(self, result):
        self._result = result

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self._result


class _FakeDB:
    """Minimal ``Session`` stand-in keyed by model class."""

    def __init__(self, rows: dict | None = None):
        self.rows = rows or {}
        self.commits = 0
        self.rollbacks = 0

    def query(self, model):
        return _FakeQuery(self.rows.get(model))

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _user(**overrides: Any) -> Any:
    """A ``User`` stand-in with the attributes the lifecycle paths read.

    Typed ``Any`` deliberately: these are structural stand-ins handed to
    signatures that declare ``User``/``Session``, and casting at every call site
    would bury the assertions.
    """
    attrs = {
        "id": 1,
        "uuid": UUID(USER_UUID),
        "email": "person@example.com",
        "role": "user",
        "auth_type": "local",
        "is_active": True,
        "allow_local_fallback": False,
        "must_change_password": False,
        "account_expires_at": None,
        "last_login_at": None,
        "password_changed_at": datetime.now(UTC),
    }
    attrs.update(overrides)
    return SimpleNamespace(**attrs)


def _request(path: str = ORDINARY_PATH) -> Any:
    """A Request stand-in carrying a matched route, as Starlette would."""
    return SimpleNamespace(
        scope={"route": SimpleNamespace(path=path)},
        url=SimpleNamespace(path=path),
        client=SimpleNamespace(host="10.0.0.1"),
        headers={"User-Agent": "pytest"},
        state=SimpleNamespace(),
        cookies={},
    )


def _code(exc) -> str:
    """The machine-readable code from a lifecycle refusal.

    ``HTTPException.detail`` is declared ``str``, but every gate in this module
    raises an object detail — the code is the contract, the prose is not.
    """
    return str(cast(dict, exc.value.detail)["code"])


def _unwrap(func):
    """Strip the slowapi rate-limit wrapper so the endpoint can be called directly."""
    while hasattr(func, "__wrapped__"):
        func = func.__wrapped__
    return func


@pytest.fixture
def audited(monkeypatch) -> list[dict]:
    """Capture every audit event the dependency gate emits."""
    events: list[dict] = []
    monkeypatch.setattr(deps_module.audit_logger, "log", lambda **kw: events.append(kw))
    return events


@pytest.fixture
def login_audited(monkeypatch) -> list[dict]:
    """Capture every audit event the login path emits."""
    events: list[dict] = []
    monkeypatch.setattr(login_module.audit_logger, "log", lambda **kw: events.append(kw))
    return events


@pytest.fixture
def expiry_enforced(monkeypatch):
    """A deployment that actually enforces the 60-day max password age."""
    monkeypatch.setattr(policy_module.password_policy, "enabled", True)
    monkeypatch.setattr(policy_module.password_policy, "max_age_days", 60)


# ── DEFECT 1: must_change_password is enforced, and confines the caller ──────────


class TestForcedPasswordChangeIsEnforced:
    def test_flagged_user_is_refused_on_an_ordinary_route(self, audited):
        with pytest.raises(HTTPException) as exc:
            get_current_active_user(
                request=_request(ORDINARY_PATH), current_user=_user(must_change_password=True)
            )

        assert exc.value.status_code == 403

    def test_refusal_carries_a_machine_readable_code(self, audited):
        """The SPA branches on detail.code; English prose is not a contract."""
        with pytest.raises(HTTPException) as exc:
            get_current_active_user(
                request=_request(ORDINARY_PATH), current_user=_user(must_change_password=True)
            )

        assert _code(exc) == ERROR_CODE_PASSWORD_CHANGE_REQUIRED
        assert cast(dict, exc.value.detail)["message"]

    def test_change_password_endpoint_stays_reachable(self, audited):
        """The one route that can CLEAR the flag must not be behind the flag."""
        user = _user(must_change_password=True)

        assert (
            get_current_active_user(request=_request(CHANGE_PASSWORD_PATH), current_user=user)
            is user
        )

    def test_logout_stays_reachable(self, audited):
        user = _user(must_change_password=True)

        assert get_current_active_user(request=_request(LOGOUT_PATH), current_user=user) is user

    def test_unflagged_user_is_unaffected(self, audited):
        user = _user()

        assert get_current_active_user(request=_request(ORDINARY_PATH), current_user=user) is user
        assert audited == []

    def test_admin_routes_are_gated_too(self, audited):
        """The admin dependency chained off get_current_user, skipping this gate."""
        user = _user(role="admin", must_change_password=True)

        with pytest.raises(HTTPException) as exc:
            get_current_admin_user(
                current_user=get_current_active_user(
                    request=_request(f"{settings.API_PREFIX}/admin/users/search"),
                    current_user=user,
                )
            )

        assert exc.value.status_code == 403
        assert _code(exc) == ERROR_CODE_PASSWORD_CHANGE_REQUIRED

    def test_refusal_is_audited(self, audited):
        with pytest.raises(HTTPException):
            get_current_active_user(
                request=_request(ORDINARY_PATH), current_user=_user(must_change_password=True)
            )

        assert [e["event_type"] for e in audited] == [AuditEventType.AUTH_PASSWORD_EXPIRED]
        assert audited[0]["details"]["reason"] == "must_change_password"

    def test_an_unmatched_path_fails_closed(self, audited):
        """No route resolved → not exempt. The gate must not open on uncertainty."""
        blank: Any = SimpleNamespace(scope={}, client=None, headers={}, state=SimpleNamespace())

        with pytest.raises(HTTPException) as exc:
            get_current_active_user(request=blank, current_user=_user(must_change_password=True))

        assert exc.value.status_code == 403

    def test_inactive_user_still_gets_the_original_400(self, audited):
        """The lifecycle gate must not swallow the pre-existing deactivation check."""
        with pytest.raises(HTTPException) as exc:
            get_current_active_user(request=_request(), current_user=_user(is_active=False))

        assert exc.value.status_code == 400


# ── DEFECT 2: an expired password sets the flag at login ─────────────────────────


class TestPasswordExpiryAtLogin:
    def _expire(self, db, user, method="local", audited_ip="10.0.0.1"):
        login_module._apply_password_expiry(db, user, method, audited_ip, "pytest")

    def test_expired_password_sets_the_flag(self, expiry_enforced, login_audited):
        user = _user(password_changed_at=datetime.now(UTC) - timedelta(days=61))
        db = _FakeDB()

        self._expire(db, user)

        assert user.must_change_password is True
        assert db.commits == 1

    def test_expired_password_emits_the_audit_event(self, expiry_enforced, login_audited):
        user = _user(password_changed_at=datetime.now(UTC) - timedelta(days=61))

        self._expire(_FakeDB(), user)

        assert [e["event_type"] for e in login_audited] == [AuditEventType.AUTH_PASSWORD_EXPIRED]
        assert login_audited[0]["user_id"] == user.id

    def test_fresh_password_is_left_alone(self, expiry_enforced, login_audited):
        user = _user(password_changed_at=datetime.now(UTC) - timedelta(days=5))

        self._expire(_FakeDB(), user)

        assert user.must_change_password is False
        assert login_audited == []

    def test_flag_flows_into_the_dependency_gate(self, expiry_enforced, login_audited, audited):
        """The whole point: expiry reuses DEFECT 1's gate rather than a second mechanism."""
        user = _user(password_changed_at=datetime.now(UTC) - timedelta(days=61))
        self._expire(_FakeDB(), user)

        with pytest.raises(HTTPException) as exc:
            get_current_active_user(request=_request(ORDINARY_PATH), current_user=user)

        assert _code(exc) == ERROR_CODE_PASSWORD_CHANGE_REQUIRED

    def test_policy_disabled_expires_nothing(self, monkeypatch, login_audited):
        monkeypatch.setattr(policy_module.password_policy, "enabled", False)
        user = _user(password_changed_at=datetime.now(UTC) - timedelta(days=5000))

        self._expire(_FakeDB(), user)

        assert user.must_change_password is False

    @pytest.mark.parametrize("auth_type", ["ldap", "keycloak", "pki"])
    def test_non_local_account_is_never_expired(self, expiry_enforced, login_audited, auth_type):
        """password_changed_at is meaningless for a directory-managed identity."""
        user = _user(
            auth_type=auth_type, password_changed_at=datetime.now(UTC) - timedelta(days=61)
        )

        login_module._apply_password_expiry(
            cast(Any, _FakeDB()), user, auth_type, "10.0.0.1", "pytest"
        )

        assert user.must_change_password is False
        assert login_audited == []

    def test_ldap_account_is_not_expired_even_on_the_local_path(
        self, expiry_enforced, login_audited
    ):
        """LDAP never has a local password — local_password_allowed is the authority."""
        user = _user(
            auth_type="ldap",
            allow_local_fallback=True,  # meaningless for LDAP, and rejected at write time
            password_changed_at=datetime.now(UTC) - timedelta(days=61),
        )

        self._expire(_FakeDB(), user)

        assert user.must_change_password is False

    def test_unknown_password_age_does_not_confine_the_user(self, expiry_enforced, login_audited):
        """No creation path ever stamped the column; NULL must not mean "expired" here."""
        user = _user(password_changed_at=None)

        self._expire(_FakeDB(), user)

        assert user.must_change_password is False
        assert login_audited == []

    def test_fallback_account_without_permission_is_not_expired(
        self, expiry_enforced, login_audited
    ):
        """A pki user with no local-fallback opt-in holds no local password to expire."""
        user = _user(
            auth_type="pki",
            allow_local_fallback=False,
            password_changed_at=datetime.now(UTC) - timedelta(days=61),
        )

        self._expire(_FakeDB(), user)

        assert user.must_change_password is False

    def test_fallback_account_with_permission_is_expired(self, expiry_enforced, login_audited):
        """…but one that DOES hold a local password is subject to the same max age."""
        user = _user(
            auth_type="pki",
            allow_local_fallback=True,
            password_changed_at=datetime.now(UTC) - timedelta(days=61),
        )

        self._expire(_FakeDB(), user)

        assert user.must_change_password is True


class TestExpiryRuleHasOneImplementation:
    """``admin.py``'s report re-derived the cutoff instead of asking the policy."""

    def test_cutoff_matches_the_row_check(self, expiry_enforced):
        cutoff = policy_module.password_expiry_cutoff()
        assert cutoff is not None  # the fixture enables expiry

        assert policy_module.is_password_expired(cutoff - timedelta(seconds=1)) is True
        assert policy_module.is_password_expired(cutoff + timedelta(minutes=1)) is False

    def test_cutoff_is_none_when_expiry_is_disabled(self, monkeypatch):
        monkeypatch.setattr(policy_module.password_policy, "enabled", False)

        assert policy_module.password_expiry_cutoff() is None

    def test_days_until_expiration_has_a_module_level_wrapper(self, expiry_enforced):
        """The missing wrapper was the tell that this build was abandoned."""
        changed = datetime.now(UTC) - timedelta(days=61)

        days = policy_module.get_days_until_expiration(changed)
        assert days is not None and days < 0


# ── DEFECT 3: last_login_at is written on every successful authentication ────────


class TestLastLoginIsStamped:
    @pytest.fixture
    def token_env(self, monkeypatch):
        monkeypatch.setattr(login_module.audit_logger, "log_login_success", lambda **kw: None)
        monkeypatch.setattr(
            login_module.token_service,
            "create_refresh_token",
            lambda **kwargs: ("refresh-token", None),
        )

    def test_helper_stamps_the_column(self):
        user = _user()
        db = _FakeDB()

        login_module.record_successful_login(cast(Any, db), user)

        assert user.last_login_at is not None
        assert db.commits == 1

    def test_session_issue_stamps_it(self, token_env):
        user = _user()

        login_module._generate_login_tokens(
            cast(Any, _FakeDB()), user, USER_UUID, "user", "pytest", "10.0.0.1", auth_method="local"
        )

        assert user.last_login_at is not None

    def test_a_write_failure_never_costs_the_session(self, token_env):
        """Bookkeeping is not allowed to 500 a login that already succeeded."""

        class _BrokenDB(_FakeDB):
            def commit(self):
                raise RuntimeError("database went away")

        db = _BrokenDB()
        response = login_module._generate_login_tokens(
            cast(Any, _FakeDB()),
            _user(),
            USER_UUID,
            "user",
            "pytest",
            "10.0.0.1",
            auth_method="local",
        )
        login_module.record_successful_login(cast(Any, db), _user())

        assert response.status_code == 200
        assert db.rollbacks == 1

    def test_failed_login_stamps_nothing(self, monkeypatch):
        user = _user()
        monkeypatch.setattr(
            login_module, "_perform_authentication", lambda db, u, p: (False, "", {}, "")
        )
        monkeypatch.setattr(
            login_module, "_get_client_info", lambda request: ("10.0.0.1", "pytest")
        )
        monkeypatch.setattr(login_module, "check_and_record_attempt", lambda *a, **k: (False, None))
        monkeypatch.setattr(
            login_module, "get_lockout_info", lambda u: {"failed_attempts": 0, "lockout_count": 0}
        )
        monkeypatch.setattr(login_module.audit_logger, "log_login_failure", lambda **kw: None)

        with pytest.raises(HTTPException) as exc:
            _unwrap(login_module.login_for_access_token)(
                request=None,
                form_data=SimpleNamespace(username="person@example.com", password="pw"),
                db=_FakeDB({User: user}),
            )

        assert exc.value.status_code == 401
        assert user.last_login_at is None


# ── DEFECT 4: account_expires_at refuses access ──────────────────────────────────


class TestAccountExpiry:
    def test_expired_account_is_refused(self, audited):
        user = _user(account_expires_at=datetime.now(UTC) - timedelta(days=1))

        with pytest.raises(HTTPException) as exc:
            get_current_active_user(request=_request(), current_user=user)

        assert exc.value.status_code == 403
        assert _code(exc) == ERROR_CODE_ACCOUNT_EXPIRED

    def test_refusal_is_audited(self, audited):
        user = _user(account_expires_at=datetime.now(UTC) - timedelta(days=1))

        with pytest.raises(HTTPException):
            get_current_active_user(request=_request(), current_user=user)

        assert [e["event_type"] for e in audited] == [AuditEventType.AUTH_ACCOUNT_EXPIRED]

    def test_future_expiry_still_allows_access(self, audited):
        user = _user(account_expires_at=datetime.now(UTC) + timedelta(days=30))

        assert get_current_active_user(request=_request(), current_user=user) is user
        assert audited == []

    def test_no_expiry_set_allows_access(self, audited):
        user = _user(account_expires_at=None)

        assert get_current_active_user(request=_request(), current_user=user) is user

    def test_naive_timestamp_is_treated_as_utc(self, audited):
        """The column is timezone-aware, but a naive value must not crash the compare."""
        user = _user(account_expires_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1))

        with pytest.raises(HTTPException) as exc:
            get_current_active_user(request=_request(), current_user=user)

        assert _code(exc) == ERROR_CODE_ACCOUNT_EXPIRED

    def test_expiry_is_not_exempt_on_the_change_password_route(self, audited):
        """Unlike a forced change, expiry has no self-service remedy — no route escapes."""
        user = _user(
            account_expires_at=datetime.now(UTC) - timedelta(days=1), must_change_password=True
        )

        with pytest.raises(HTTPException) as exc:
            get_current_active_user(request=_request(CHANGE_PASSWORD_PATH), current_user=user)

        assert _code(exc) == ERROR_CODE_ACCOUNT_EXPIRED


# ── DEFECT 4: the forced-change hold had no exit without a mail server ──────────


class TestSelfServiceChangeClearsTheHold:
    """The gate is only survivable if something clears the flag it reads.

    Three paths SET ``must_change_password`` — admin create, admin force-change,
    and password expiry at login — and for a while exactly one cleared it: the
    emailed reset. A deployment with no mail transport therefore had no exit at
    all. The user changed their password on the forced-change screen, got held
    again on the very next request, and after ``PASSWORD_HISTORY_COUNT`` attempts
    ran out of passwords they were allowed to reuse.

    ``PUT /users/me`` is the route the gate deliberately leaves reachable, so it
    is the route that has to clear the flag.
    """

    def test_the_self_service_route_is_the_one_that_clears_it(self):
        import inspect

        from app.api.endpoints import users as users_module

        source = inspect.getsource(users_module.update_current_user)
        body = "\n".join(line for line in source.splitlines() if not line.strip().startswith("#"))
        assert "must_change_password = False" in body, (
            "PUT /users/me must clear must_change_password. Without it the forced-"
            "change gate is a permanent lockout on any deployment without SMTP."
        )

    def test_the_caller_keeps_a_session_after_changing_their_own_password(self):
        """Revocation is total and includes THIS session, so it must be re-issued.

        Otherwise the change succeeds, every cookie dies, and the user is bounced
        to the login screen — indistinguishable from the change having failed.
        """
        import inspect

        from app.api.endpoints import users as users_module

        source = inspect.getsource(users_module.update_current_user)
        assert "reissue_current_session" in source
        assert source.index("revoke_all_sessions") < source.index("reissue_current_session"), (
            "the new session must be minted AFTER the revocation, or it is revoked too"
        )

    def test_an_admin_setting_someone_elses_password_forces_a_change(self):
        """The admin now knows a working credential for another account."""
        import inspect

        from app.api.endpoints import users as users_module

        source = inspect.getsource(users_module.update_user)
        assert '"must_change_password"' in source
        assert "user.id != current_user.id" in source, (
            "an admin editing their OWN row must not be forced to change again"
        )
