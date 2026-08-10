"""Audit events must be correlatable, and the gaps in coverage must stay closed.

Three defects:

* ``auth/audit.py`` and ``middleware/audit.py`` each declared their own
  ``ContextVar("request_id")``. The display name is documentation, not identity — two
  objects with the same name are two independent slots — so the middleware set one and
  the audit logger read the other, always found it empty, and substituted a fresh
  ``uuid4()`` per event. Nothing could be correlated: not login -> MFA -> session, not
  request -> failure -> lockout.
* ``auth/password_reset.py`` emitted no audit events at all. A password reset is the
  first thing a reviewer looks for when investigating a takeover.
* ``AUTH_SESSION_LIMIT_EXCEEDED`` had zero emitters; the concurrent-session eviction
  logged ``AUTH_SESSION_EXPIRED``, so AC-10 enforcement was indistinguishable from an
  ordinary timeout.
"""

from __future__ import annotations

import ast
import inspect
from typing import cast
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.api.endpoints.auth import login as login_module
from app.auth import audit as audit_module
from app.auth import password_reset as pr_module
from app.auth.audit import AuditEventType
from app.core import request_context
from app.middleware import audit as middleware_audit


@pytest.mark.unit
class TestRequestIdIsOneContextVar:
    def test_the_two_modules_share_one_object(self):
        assert audit_module.request_id_var is request_context.request_id_var
        assert middleware_audit.get_request_id.__module__ == request_context.__name__

    def test_middleware_writes_are_visible_to_the_audit_logger(self):
        """The exact regression: a value set by the middleware was invisible here."""
        middleware_audit.set_request_id("req-correlate-1")
        try:
            assert audit_module.request_id_var.get() == "req-correlate-1"
        finally:
            middleware_audit.set_request_id("")

    def test_events_in_one_request_share_an_id(self):
        """Two events emitted under one request must carry the same request_id."""
        middleware_audit.set_request_id("req-correlate-2")
        emitted = []
        try:
            with (
                patch.object(audit_module.settings, "AUDIT_LOG_TO_OPENSEARCH", False),
                patch.object(audit_module.audit_logger, "_logger") as sink,
            ):
                sink.info.side_effect = lambda msg: emitted.append(msg)
                audit_module.audit_logger.log(
                    event_type=AuditEventType.AUTH_LOGIN_SUCCESS,
                    outcome=audit_module.AuditOutcome.SUCCESS,
                )
                audit_module.audit_logger.log(
                    event_type=AuditEventType.AUTH_SESSION_CREATED,
                    outcome=audit_module.AuditOutcome.SUCCESS,
                )
        finally:
            middleware_audit.set_request_id("")

        assert len(emitted) == 2
        assert all("req-correlate-2" in line for line in emitted)

    def test_core_context_module_has_no_app_imports(self):
        """``app.core`` must not reach into ``app.api`` / ``app.services``.

        AST rather than substring, so the module docstring may explain the rule.
        """
        imported = _module_imports(request_context)
        assert not any(name.startswith(("app.api", "app.services")) for name in imported), imported


@pytest.mark.unit
class TestPasswordResetIsAudited:
    def test_request_for_an_unknown_address_is_audited(self):
        """Every exit path emits, so the audit write is not itself a timing oracle."""
        db = _EmptySession()
        with patch.object(pr_module.audit_logger, "log") as log:
            pr_module.request_password_reset(cast(Session, db), "nobody@example.com", "10.0.0.1")

        assert log.called
        assert log.call_args.kwargs["event_type"] == AuditEventType.AUTH_PASSWORD_RESET_REQUEST
        assert log.call_args.kwargs["outcome"] == pr_module.AuditOutcome.FAILURE

    def test_unknown_address_is_not_written_into_the_audit_record(self):
        """An attacker-supplied string is a log-injection surface with no value here."""
        db = _EmptySession()
        with patch.object(pr_module.audit_logger, "log") as log:
            pr_module.request_password_reset(cast(Session, db), "nobody@example.com", "10.0.0.1")

        assert log.call_args.kwargs["username"] is None
        assert log.call_args.kwargs["user_id"] is None

    def test_successful_request_is_audited(self):
        db = _ResetSession()
        with (
            patch.object(pr_module.audit_logger, "log") as log,
            patch.object(pr_module.email_service, "send_password_reset"),
        ):
            pr_module.request_password_reset(cast(Session, db), "user@example.com", "10.0.0.1")

        assert log.call_args.kwargs["outcome"] == pr_module.AuditOutcome.SUCCESS
        assert log.call_args.kwargs["error_code"] is None

    def test_completion_is_audited_with_the_purpose_built_event(self):
        db = _ConfirmSession()
        with (
            patch.object(pr_module.audit_logger, "log") as log,
            patch.object(pr_module, "check_password_against_history", return_value=True),
            patch.object(pr_module, "add_password_to_history"),
            patch.object(pr_module.settings, "PASSWORD_POLICY_ENABLED", False),
            patch.object(
                pr_module.token_service, "revoke_all_user_tokens_in_transaction", return_value=0
            ),
        ):
            ok, _ = pr_module.confirm_password_reset(
                cast(Session, db), "raw-token", "NewPassw0rd!x"
            )

        assert ok is True
        assert log.call_args.kwargs["event_type"] == AuditEventType.AUTH_PASSWORD_RESET_COMPLETE
        assert log.call_args.kwargs["outcome"] == pr_module.AuditOutcome.SUCCESS

    def test_invalid_token_is_audited_without_leaking_the_token(self):
        db = _EmptySession()
        with patch.object(pr_module.audit_logger, "log") as log:
            ok, _ = pr_module.confirm_password_reset(cast(Session, db), "bogus", "NewPassw0rd!x")

        assert ok is False
        record = log.call_args.kwargs
        assert record["event_type"] == AuditEventType.AUTH_PASSWORD_RESET_COMPLETE
        assert "bogus" not in str(record), "a reset token must never reach the audit index"

    def test_uses_the_existing_event_vocabulary(self):
        """No invented event names."""
        source = inspect.getsource(pr_module)
        for name in ("AUTH_PASSWORD_RESET_REQUEST", "AUTH_PASSWORD_RESET_COMPLETE"):
            assert name in source
            assert hasattr(AuditEventType, name)


@pytest.mark.unit
class TestSessionLimitEventIsUsed:
    def test_concurrent_limit_no_longer_logs_session_expired(self):
        """An eviction is not an expiry. AST, so explanatory comments don't count."""
        emitted = _audit_event_names(login_module)
        assert "AUTH_SESSION_LIMIT_EXCEEDED" in emitted
        assert "AUTH_SESSION_EXPIRED" not in emitted

    def test_both_policies_emit_the_event(self):
        """``reject`` was silent entirely; ``terminate_oldest`` used the wrong event."""
        assert _audit_event_names(login_module).count("AUTH_SESSION_LIMIT_EXCEEDED") == 2


# ---------------------------------------------------------------------------
# Structural helpers (AST, so prose in comments and docstrings never matches).
# ---------------------------------------------------------------------------


def _module_imports(module) -> list[str]:
    """Return the dotted names a module imports at any level."""
    tree = ast.parse(inspect.getsource(module))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def _audit_event_names(module) -> list[str]:
    """Return every ``AuditEventType.X`` referenced in real code in ``module``."""
    tree = ast.parse(inspect.getsource(module))
    return [
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "AuditEventType"
    ]


# ---------------------------------------------------------------------------
# Minimal fakes — these exercise audit emission, not SQL.
# ---------------------------------------------------------------------------


class _FakeQuery:
    def __init__(self, result, count=0):
        self._result = result
        self._count = count

    def filter(self, *_a, **_k):
        return self

    def first(self):
        return self._result

    def count(self):
        return self._count

    def update(self, *_a, **_k):
        return 0


class _EmptySession:
    def query(self, _model):
        return _FakeQuery(None)

    def add(self, _obj):
        pass

    def commit(self):
        pass

    def rollback(self):
        pass


class _FakeUser:
    def __init__(self):
        self.id = 1
        self.email = "user@example.com"
        self.auth_type = "local"
        self.is_active = True
        self.hashed_password = "sentinel-not-a-credential"
        self.password_changed_at = None
        self.must_change_password = True


class _ResetSession(_EmptySession):
    """A local, active user with no recent reset tokens."""

    def __init__(self):
        self.user = _FakeUser()

    def query(self, model):
        if "User" in getattr(model, "__name__", ""):
            return _FakeQuery(self.user)
        return _FakeQuery(None, count=0)


class _FakeToken:
    def __init__(self):
        self.id = 7
        self.user_id = 1
        self.used_at = None


class _ConfirmSession(_EmptySession):
    def __init__(self):
        self.user = _FakeUser()
        self.token = _FakeToken()

    def query(self, model):
        if "User" in getattr(model, "__name__", ""):
            return _FakeQuery(self.user)
        return _FakeQuery(self.token)
