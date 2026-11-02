"""A login attempt whose account lookup fails is refused, not counted on a guess.

``_resolve_lockout_account`` returned ``None`` when its query raised, which the
handler could not tell from "no such account": the lockout bucket fell back to the
submitted string, so each alias of one account (email, ldap_uid) got its own counter
while the lookup was failing, and a correct password via an alias cleared or skipped
the account's own lock.

Now a lookup failure is its own outcome. The attempt is refused with one 503 whatever
the password, a failed attempt is still counted on the normalised submitted
identifier and audited, and a correct one is neither counted nor allowed to clear a
counter. These are pure unit tests: the collaborators are replaced through
``login.py`` globals, so nothing touches a database, Redis or the rate limiter.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from fastapi import HTTPException
from fastapi import Response

from app.api.endpoints.auth import login as login_module

pytestmark = pytest.mark.unit

USER_UUID = "11111111-2222-3333-4444-555555555555"


def _unwrap(func):
    """Strip the slowapi rate-limit wrapper so the endpoint can be called directly."""
    while hasattr(func, "__wrapped__"):
        func = func.__wrapped__
    return func


class _FailingQuery:
    """A session whose every query raises, as during a database outage."""

    def query(self, *_args, **_kwargs):
        raise RuntimeError("database unavailable")


class TestTheResolver:
    def test_a_failing_lookup_raises_instead_of_reading_as_no_account(self):
        with pytest.raises(login_module.AccountLookupError):
            login_module._resolve_lockout_account(cast(Any, _FailingQuery()), "jdoe", "")

    def test_a_failing_lookup_by_uuid_raises_too(self):
        with pytest.raises(login_module.AccountLookupError):
            login_module._resolve_lockout_account(cast(Any, _FailingQuery()), "jdoe", USER_UUID)


class TestTheHandler:
    @pytest.fixture
    def env(self, monkeypatch):
        """Record what the handler counts and audits. The account lookup runs for
        real against a session whose queries raise."""
        seen: dict[str, list] = {"recorded": [], "audited": [], "reached": []}

        def _record(identifier, success, exempt_from_lockout=False):
            seen["recorded"].append((identifier, success, exempt_from_lockout))
            return False, None

        def _role(db, uuid_str, data):
            seen["reached"].append("role")
            return "user"

        monkeypatch.setattr(login_module, "check_and_record_attempt", _record)
        monkeypatch.setattr(
            login_module,
            "get_lockout_info",
            lambda ident: {"failed_attempts": 1, "lockout_count": 0},
        )
        monkeypatch.setattr(
            login_module.audit_logger,
            "log_login_failure",
            lambda **kw: seen["audited"].append(kw),
        )
        monkeypatch.setattr(
            login_module, "_get_client_info", lambda request: ("10.0.0.1", "pytest")
        )
        monkeypatch.setattr(
            login_module,
            "get_auth_settings",
            lambda db: SimpleNamespace(
                account_lockout_threshold=5, account_lockout_duration_minutes=15
            ),
        )
        monkeypatch.setattr(login_module, "_get_user_role", _role)
        return seen

    def _call(self, monkeypatch, *, auth_success: bool, username: str = " JDoe "):
        result: tuple[bool, str, dict, str] = (
            (True, USER_UUID, {}, "local") if auth_success else (False, "", {}, "")
        )
        monkeypatch.setattr(login_module, "_perform_authentication", lambda db, u, p: result)
        return _unwrap(login_module.login_for_access_token)(
            request=cast(Any, None),
            response=Response(),
            form_data=SimpleNamespace(username=username, password="pw"),
            db=cast(Any, _FailingQuery()),
        )

    def test_a_wrong_password_is_counted_on_the_submitted_identifier(self, monkeypatch, env):
        with pytest.raises(HTTPException) as caught:
            self._call(monkeypatch, auth_success=False)

        assert caught.value.status_code == 503
        assert env["recorded"] == [("jdoe", False, False)]
        assert [a["error_code"] for a in env["audited"]] == ["INVALID_CREDENTIALS"]

    def test_a_correct_password_is_refused_with_the_same_answer(self, monkeypatch, env):
        with pytest.raises(HTTPException) as caught:
            self._call(monkeypatch, auth_success=True)

        assert caught.value.status_code == 503
        assert env["recorded"] == [], "a correct password must not clear or bump a counter"
        assert env["reached"] == [], "an unresolved login went on to build a session"
        assert [a["error_code"] for a in env["audited"]] == ["ACCOUNT_LOOKUP_FAILED"]

    def test_both_outcomes_share_one_body(self, monkeypatch, env):
        """The response must not say whether the password was right."""
        with pytest.raises(HTTPException) as wrong:
            self._call(monkeypatch, auth_success=False)
        with pytest.raises(HTTPException) as right:
            self._call(monkeypatch, auth_success=True)

        assert wrong.value.detail == right.value.detail
        assert wrong.value.status_code == right.value.status_code == 503

    def test_a_resolved_miss_still_takes_the_ordinary_path(self, monkeypatch, env):
        """Control: "no such account" is not a lookup failure; it stays a uniform 401
        and is counted on the submitted identifier, as before."""
        monkeypatch.setattr(login_module, "_resolve_lockout_account", lambda db, u, s: None)

        with pytest.raises(HTTPException) as caught:
            self._call(monkeypatch, auth_success=False)

        assert caught.value.status_code == 401
        assert caught.value.detail == "Incorrect username or password"
        assert env["recorded"] == [("jdoe", False, False)]
