"""The login endpoint's LDAP branches in ``app.api.endpoints.auth.authenticators``.

``_authenticate_ldap_user`` turns ``ldap_authenticate`` / ``sync_ldap_user_to_db`` results
into the endpoint's answers, and ``_authenticate_production_user`` decides whether a password
is tried locally first, against the directory first, or both. Neither had a test that drove
those branches directly. Collaborators are replaced through the module's globals, so nothing
here touches a database or a directory.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.api.endpoints.auth import authenticators
from app.core import auth_settings as auth_settings_module
from app.core.exceptions import AuthenticationError
from app.models.user import User

pytestmark = pytest.mark.unit

USER_UUID = "0b7c3d4e-1f2a-4b5c-8d9e-0f1a2b3c4d5e"
REFUSAL = "Incorrect username or password"
BEARER = {"WWW-Authenticate": "Bearer"}
LDAP_DATA = {
    "username": "alice",
    "email": "alice@test.example",
    "full_name": "Alice",
    "is_admin": False,
    "groups": [],
}


def _row(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "uuid": USER_UUID,
        "email": "alice@test.example",
        "full_name": "Alice",
        "role": "user",
        "is_active": True,
        "is_superuser": False,
        "auth_type": "ldap",
        "allow_local_fallback": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _ldap_enabled(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    # The function imports get_auth_settings inside its body, so patch the source module.
    monkeypatch.setattr(
        auth_settings_module,
        "get_auth_settings",
        lambda db: SimpleNamespace(ldap_enabled=enabled),
    )


class _Query:
    def __init__(self, found: Any) -> None:
        self.found = found
        self.criteria: list[Any] = []

    def filter(self, *criteria: Any) -> _Query:
        self.criteria.extend(criteria)
        return self

    def first(self) -> Any:
        return self.found


class _Db:
    def __init__(self, found: Any = None) -> None:
        self.query_made = _Query(found)
        self.models: list[Any] = []

    def query(self, model: Any) -> _Query:
        self.models.append(model)
        return self.query_made


def _session(db: _Db) -> Session:
    return cast(Session, db)


class TestAuthenticateLdapUser:
    def test_ldap_disabled_is_a_401_and_the_directory_is_never_asked(self, monkeypatch):
        _ldap_enabled(monkeypatch, False)

        def _never(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("the directory was asked while LDAP is disabled")

        monkeypatch.setattr(authenticators, "ldap_authenticate", _never)

        with pytest.raises(HTTPException) as raised:
            authenticators._authenticate_ldap_user(_session(_Db()), "alice", "pw")

        assert (raised.value.status_code, raised.value.detail, raised.value.headers) == (
            401,
            "LDAP authentication is not enabled",
            BEARER,
        )

    def test_a_refused_directory_login_is_the_uniform_401_and_nothing_is_synced(
        self, monkeypatch, caplog
    ):
        _ldap_enabled(monkeypatch, True)
        seen: list[tuple[str, str, Any]] = []

        def _refuse(username: str, password: str, db: Any = None) -> None:
            seen.append((username, password, db))
            return None

        def _never(*args: Any) -> Any:
            raise AssertionError("a refused login was synced")

        monkeypatch.setattr(authenticators, "ldap_authenticate", _refuse)
        monkeypatch.setattr(authenticators, "sync_ldap_user_to_db", _never)
        db = _Db()

        with caplog.at_level(logging.WARNING), pytest.raises(HTTPException) as raised:
            authenticators._authenticate_ldap_user(_session(db), "alice", "pw")

        assert (raised.value.status_code, raised.value.detail, raised.value.headers) == (
            401,
            REFUSAL,
            BEARER,
        )
        assert seen == [("alice", "pw", db)]
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("alice" in m for m in warnings)

    def test_success_syncs_the_directory_answer_and_returns_the_token_fields(
        self, monkeypatch, caplog
    ):
        _ldap_enabled(monkeypatch, True)
        synced: list[tuple[Any, Any]] = []
        row = _row(role="admin", is_superuser=False)

        def _sync(db: Any, data: Any) -> Any:
            synced.append((db, data))
            return row

        monkeypatch.setattr(authenticators, "ldap_authenticate", lambda u, p, db=None: LDAP_DATA)
        monkeypatch.setattr(authenticators, "sync_ldap_user_to_db", _sync)
        db = _Db()

        with caplog.at_level(logging.INFO):
            result = authenticators._authenticate_ldap_user(_session(db), "alice", "pw")

        assert synced == [(db, LDAP_DATA)]
        assert result == (
            USER_UUID,
            {
                "uuid": USER_UUID,
                "email": "alice@test.example",
                "full_name": "Alice",
                "role": "admin",
                "is_active": True,
                "is_superuser": False,
            },
        )
        infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert any("alice" in m for m in infos)

    def test_an_inactive_synced_account_is_a_400(self, monkeypatch, caplog):
        """P3 D-14 (out of scope, kept): unlike every other refusal this is not the 401."""
        _ldap_enabled(monkeypatch, True)
        monkeypatch.setattr(authenticators, "ldap_authenticate", lambda u, p, db=None: LDAP_DATA)
        monkeypatch.setattr(
            authenticators, "sync_ldap_user_to_db", lambda db, data: _row(is_active=False)
        )

        with caplog.at_level(logging.WARNING), pytest.raises(HTTPException) as raised:
            authenticators._authenticate_ldap_user(_session(_Db()), "alice", "pw")

        assert (raised.value.status_code, raised.value.detail) == (400, "Inactive user account")
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("alice" in m for m in warnings)


def test_build_user_data_holds_exactly_the_token_fields():
    row = _row(role="super_admin", is_superuser=True, is_active=False)

    assert authenticators._build_user_data(cast(User, row)) == {
        "uuid": USER_UUID,
        "email": "alice@test.example",
        "full_name": "Alice",
        "role": "super_admin",
        "is_active": False,
        "is_superuser": True,
    }


class TestProductionLoginOrder:
    """``_authenticate_production_user``: which source is tried first, and the fallbacks."""

    @pytest.fixture
    def calls(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        state: dict[str, Any] = {
            "log": [],
            "local": None,
            "ldap": ("ldap-uuid", {"via": "ldap"}),
        }

        def _local(db: Any, username: str, password: str) -> Any:
            state["log"].append(("local", username, password))
            return state["local"]

        def _ldap(db: Any, username: str, password: str) -> Any:
            state["log"].append(("ldap", username, password))
            outcome = state["ldap"]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(authenticators, "_authenticate_local_user", _local)
        monkeypatch.setattr(authenticators, "_authenticate_ldap_user", _ldap)
        monkeypatch.setattr(authenticators, "_local_auth_permitted", lambda db, user: True)
        return state

    def test_the_account_is_looked_up_by_email_or_ldap_uid(self, calls):
        db = _Db(found=None)

        authenticators._authenticate_production_user(_session(db), "typed-name", "pw")

        assert db.models == [User]
        (criterion,) = db.query_made.criteria
        compiled = criterion.compile(dialect=postgresql.dialect())
        assert " OR " in str(compiled)
        assert '"user".email' in str(compiled) and '"user".ldap_uid' in str(compiled)
        assert set(compiled.params.values()) == {"typed-name"}

    def test_with_no_account_only_the_directory_is_tried(self, calls):
        result = authenticators._authenticate_production_user(_session(_Db()), "alice", "pw")

        assert result == ("ldap-uuid", {"via": "ldap"}, "ldap")
        assert calls["log"] == [("ldap", "alice", "pw")]

    def test_with_no_account_a_directory_refusal_propagates_unchanged(self, calls):
        refusal = HTTPException(status_code=401, detail=REFUSAL, headers=BEARER)
        calls["ldap"] = refusal

        with pytest.raises(HTTPException) as raised:
            authenticators._authenticate_production_user(_session(_Db()), "alice", "pw")

        assert raised.value is refusal

    def test_a_local_account_is_tried_locally_first(self, calls):
        calls["local"] = ("local-uuid", {"via": "local"})
        db = _Db(found=_row(auth_type="local"))

        result = authenticators._authenticate_production_user(_session(db), "alice", "pw")

        assert result == ("local-uuid", {"via": "local"}, "local")
        assert calls["log"] == [("local", "alice", "pw")]

    def test_a_local_account_falls_back_to_the_directory(self, calls):
        db = _Db(found=_row(auth_type="local"))

        result = authenticators._authenticate_production_user(_session(db), "alice", "pw")

        assert result == ("ldap-uuid", {"via": "ldap"}, "ldap")
        assert calls["log"] == [("local", "alice", "pw"), ("ldap", "alice", "pw")]

    def test_a_local_accounts_directory_fallback_refusal_propagates(self, calls):
        refusal = HTTPException(status_code=401, detail=REFUSAL, headers=BEARER)
        calls["ldap"] = refusal
        db = _Db(found=_row(auth_type="local"))

        with pytest.raises(HTTPException) as raised:
            authenticators._authenticate_production_user(_session(db), "alice", "pw")

        assert raised.value is refusal

    @pytest.mark.parametrize(
        "refusal",
        [
            HTTPException(status_code=401, detail=REFUSAL, headers=BEARER),
            AuthenticationError(REFUSAL),
        ],
        ids=["http-401", "domain-auth-error"],
    )
    def test_a_directory_account_falls_back_to_a_local_password_by_stored_email(
        self, calls, refusal
    ):
        calls["ldap"] = refusal
        calls["local"] = ("local-uuid", {"via": "local"})
        db = _Db(found=_row(auth_type="ldap", email="stored@test.example"))

        result = authenticators._authenticate_production_user(_session(db), "alice", "pw")

        assert result == ("local-uuid", {"via": "local"}, "local")
        assert calls["log"] == [("ldap", "alice", "pw"), ("local", "stored@test.example", "pw")]

    def test_when_both_refuse_the_answer_is_the_uniform_401_without_a_cause(self, calls):
        calls["ldap"] = HTTPException(status_code=401, detail="directory said no")
        db = _Db(found=_row(auth_type="ldap"))

        with pytest.raises(HTTPException) as raised:
            authenticators._authenticate_production_user(_session(db), "alice", "pw")

        assert (raised.value.status_code, raised.value.detail, raised.value.headers) == (
            401,
            REFUSAL,
            BEARER,
        )
        assert raised.value.__cause__ is None
        assert raised.value.__suppress_context__ is True

    def test_an_unexpected_directory_error_is_not_swallowed(self, calls):
        calls["ldap"] = RuntimeError("not an authentication failure")
        db = _Db(found=_row(auth_type="ldap"))

        with pytest.raises(RuntimeError):
            authenticators._authenticate_production_user(_session(db), "alice", "pw")

        assert calls["log"] == [("ldap", "alice", "pw")]
