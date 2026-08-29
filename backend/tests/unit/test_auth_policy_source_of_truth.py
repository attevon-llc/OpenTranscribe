"""Enforcement and reporting must read the SAME auth policy.

The auth config plane is DB-backed (admin UI, resolving DB > .env > coded default).
Three call sites had not moved with it, and each failure mode is different:

1. ``password_reset.confirm_password_reset`` gated ``validate_password`` behind
   ``settings.PASSWORD_POLICY_ENABLED`` — the **.env** value. Since
   ``validate_password`` already no-ops when the policy is off, the extra gate could
   only ever *subtract*: a deployment that switched the policy on in the admin UI
   while .env still said false got no validation at all, on the one path where a weak
   password is most likely to be chosen. A real bypass, not a reporting bug.
2. The account-lockout audit event quoted ``settings.ACCOUNT_LOCKOUT_*``, so the
   compliance artefact stated a threshold and duration the system had not applied.
3. The password-expiry response returned ``settings.PASSWORD_MAX_AGE_DAYS``, telling
   the user a max age that was not the one enforced.

These tests drive each site with the .env value and the DB value **deliberately
disagreeing**, and assert the DB value wins. A test that leaves them equal passes
either way and proves nothing.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.api.endpoints.auth import login as login_module
from app.auth import password_reset as pr_module
from app.core.auth_settings import DynamicAuthSettings
from app.models.user import User


@pytest.mark.unit
class TestResetPathCannotBypassThePasswordPolicy:
    def test_policy_runs_even_when_the_env_flag_is_false(self):
        """The exact bypass: DB says enforce, .env says don't, reset skipped it."""
        db = _ConfirmSession()

        with (
            patch.object(pr_module.settings, "PASSWORD_POLICY_ENABLED", False),
            patch.object(pr_module, "validate_password") as validate,
            patch.object(pr_module.audit_logger, "log"),
            patch.object(pr_module, "check_password_against_history", return_value=True),
            patch.object(pr_module, "add_password_to_history"),
            patch.object(
                pr_module.token_service, "revoke_all_user_tokens_in_transaction", return_value=0
            ),
        ):
            validate.return_value = SimpleNamespace(is_valid=True, errors=[])
            pr_module.confirm_password_reset(cast(Session, db), "raw-token", "weak")

        assert validate.call_count == 1, (
            "the reset path must consult the policy resolver, not the .env flag"
        )
        # And consult it about the password being SET. `.called` is equally true of a
        # call that validated the old password, or the token, and then let "weak" through.
        assert validate.call_args.args[0] == "weak"

    def test_a_policy_rejection_still_fails_the_reset(self):
        db = _ConfirmSession()

        with (
            patch.object(pr_module.settings, "PASSWORD_POLICY_ENABLED", False),
            patch.object(pr_module, "validate_password") as validate,
            patch.object(pr_module.audit_logger, "log"),
        ):
            validate.return_value = SimpleNamespace(is_valid=False, errors=["too short"])
            ok, errors = pr_module.confirm_password_reset(cast(Session, db), "raw-token", "weak")

        assert ok is False
        assert errors == ["too short"]

    def test_the_env_gate_is_gone(self):
        """Structural guard: reintroducing the guard reintroduces the bypass."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(pr_module))
        read_names = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "settings"
        }
        assert "PASSWORD_POLICY_ENABLED" not in read_names


@pytest.mark.unit
class TestAuditQuotesTheEnforcedLockoutPolicy:
    def test_lockout_event_reports_the_resolved_values_not_env(self):
        policy = SimpleNamespace(
            account_lockout_threshold=3,  # DB
            account_lockout_duration_minutes=42,  # DB
        )
        captured: dict = {}

        with (
            patch.object(login_module.settings, "ACCOUNT_LOCKOUT_THRESHOLD", 99),
            patch.object(login_module.settings, "ACCOUNT_LOCKOUT_DURATION_MINUTES", 1),
            patch.object(
                login_module, "check_and_record_attempt", return_value=(True, _in_an_hour())
            ),
            patch.object(
                login_module,
                "get_lockout_info",
                return_value={"failed_attempts": 3, "lockout_count": 1},
            ),
            patch.object(
                login_module.audit_logger,
                "log_account_lockout",
                side_effect=lambda **kw: captured.update(kw),
            ),
        ):
            login_module._handle_lockout_check(
                "person@example.com",
                "person@example.com",
                False,
                "10.0.0.1",
                "pytest",
                cast(DynamicAuthSettings, policy),
            )

        assert captured, "a just-locked account must produce a lockout audit event"
        assert captured["lockout_duration_minutes"] == 42, (
            "the audit record must state the duration that was enforced"
        )

    def test_a_higher_env_threshold_cannot_suppress_the_event(self):
        """With the .env threshold at 99 the old code never logged the lockout."""
        policy = SimpleNamespace(account_lockout_threshold=3, account_lockout_duration_minutes=42)
        calls = []

        with (
            patch.object(login_module.settings, "ACCOUNT_LOCKOUT_THRESHOLD", 99),
            patch.object(
                login_module, "check_and_record_attempt", return_value=(True, _in_an_hour())
            ),
            patch.object(
                login_module,
                "get_lockout_info",
                return_value={"failed_attempts": 3, "lockout_count": 1},
            ),
            patch.object(
                login_module.audit_logger,
                "log_account_lockout",
                side_effect=lambda **kw: calls.append(kw),
            ),
        ):
            login_module._handle_lockout_check(
                "person@example.com",
                "person@example.com",
                False,
                "1.2.3.4",
                "pytest",
                cast(DynamicAuthSettings, policy),
            )

        assert len(calls) == 1


@pytest.mark.unit
class TestPasswordExpiryReportsTheEnforcedMaxAge:
    def test_response_carries_the_resolved_max_age(self):
        policy = SimpleNamespace(password_max_age_days=30)  # DB
        user = SimpleNamespace(
            id=1,
            email="user@example.com",
            auth_type="local",
            allow_local_fallback=False,
            password_changed_at=datetime.now(UTC) - timedelta(days=400),
            must_change_password=True,
        )
        captured: dict = {}

        with (
            patch.object(login_module.settings, "PASSWORD_MAX_AGE_DAYS", 365),
            patch.object(login_module, "is_password_expired", return_value=True),
            patch.object(login_module, "get_days_until_expiration", return_value=-370),
            patch.object(login_module, "local_password_allowed", return_value=(True, "")),
            patch.object(
                login_module.audit_logger, "log", side_effect=lambda **kw: captured.update(kw)
            ),
        ):
            login_module._apply_password_expiry(
                cast(Session, _NoopSession()),
                cast(User, user),
                "local",
                "10.0.0.1",
                "pytest",
                cast(DynamicAuthSettings, policy),
            )

        assert captured["details"]["max_age_days"] == 30, (
            "the user must be told the max age that was actually applied"
        )


def _in_an_hour() -> datetime:
    return datetime.now(UTC) + timedelta(hours=1)


class _NoopSession:
    def commit(self):
        pass


class _FakeQuery:
    def __init__(self, result):
        self._result = result

    def filter(self, *_a, **_k):
        return self

    def first(self):
        return self._result

    def update(self, *_a, **_k):
        return 0


class _ConfirmSession:
    """A valid, unused reset token belonging to a local user."""

    def __init__(self):
        self.user = SimpleNamespace(
            id=1,
            email="user@example.com",
            hashed_password="sentinel-not-a-credential",
            password_changed_at=None,
            must_change_password=True,
        )
        self.token = SimpleNamespace(id=7, user_id=1, used_at=None)

    def query(self, model):
        if "User" in getattr(model, "__name__", ""):
            return _FakeQuery(self.user)
        return _FakeQuery(self.token)

    def add(self, _obj):
        pass

    def commit(self):
        pass

    def rollback(self):
        pass
