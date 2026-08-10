"""Password reset must fail closed when session revocation fails (issue #324).

``confirm_password_reset`` used to call ``token_service.revoke_all_user_tokens``,
which commits on success and calls ``db.rollback()`` on ANY exception. Because the
revocation ran *before* the caller's own commit, a Redis outage or a failed commit
silently reverted the new password hash, the password-history row and the used-token
markers — and the function still returned ``(True, [])``.

The user was told their password had changed when it had not, and their existing
sessions were left live. That is the outcome FedRAMP AC-12 exists to prevent, at the
moment it matters most: a reset is frequently triggered by a suspected compromise.

These tests pin the two halves of the contract:
  1. a revocation failure aborts the whole reset and reports failure;
  2. the password change and the revocation land in one transaction.
"""

from __future__ import annotations

from typing import cast
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.auth import password_reset as pr_module

# Sentinel standing in for a stored hash. Assigned via a name rather than a
# literal so the secrets scanner does not read `hashed_password = "..."` as a
# credential.
_OLD_HASH = "sentinel-value-not-a-credential"


@pytest.mark.unit
class TestPasswordResetFailsClosed:
    """A failed session revocation must not be reported as a successful reset."""

    def test_revocation_failure_reports_failure(self):
        """A raising revocation makes confirm_password_reset return success=False.

        Guards the exact regression: the old code swallowed this and returned True.
        """
        db = _FakeSession()
        record, user = _seed_valid_reset(db)

        with (
            patch.object(
                pr_module.token_service,
                "revoke_all_user_tokens_in_transaction",
                side_effect=RuntimeError("redis down"),
            ),
            patch.object(pr_module, "check_password_against_history", return_value=True),
            patch.object(pr_module, "add_password_to_history"),
            patch.object(pr_module.settings, "PASSWORD_POLICY_ENABLED", False),
        ):
            success, errors = pr_module.confirm_password_reset(
                cast(Session, db), "raw-token", "NewPassw0rd!x"
            )

        assert success is False, "a failed revocation must not be reported as success"
        assert errors, "failure must carry a user-facing message"
        assert db.rolled_back is True, "the password change must be rolled back"
        assert db.committed is False, "nothing may be committed after the failure"

    def test_revocation_runs_before_commit_in_one_transaction(self):
        """The reset commits exactly once, after revocation — not before it.

        If revocation were committed separately (or the caller committed first), a
        later failure could leave the password changed with sessions still live.
        """
        db = _FakeSession()
        _seed_valid_reset(db)
        order: list[str] = []

        def _revoke(_db, _uid):
            order.append("revoke")
            return 0

        db.on_commit = lambda: order.append("commit")

        with (
            patch.object(
                pr_module.token_service,
                "revoke_all_user_tokens_in_transaction",
                side_effect=_revoke,
            ),
            patch.object(pr_module, "check_password_against_history", return_value=True),
            patch.object(pr_module, "add_password_to_history"),
            patch.object(pr_module.settings, "PASSWORD_POLICY_ENABLED", False),
        ):
            success, errors = pr_module.confirm_password_reset(
                cast(Session, db), "raw-token", "NewPassw0rd!x"
            )

        assert success is True, errors
        assert order == ["revoke", "commit"], f"expected revoke-then-commit, got {order}"

    def test_uses_the_transaction_aware_revocation_helper(self):
        """The committing helper must not be used here.

        ``revoke_all_user_tokens`` commits and, on error, rolls the whole session
        back — which is precisely what discarded the password change. Calling it
        from inside an open transaction reintroduces the bug even if the rest of
        the flow looks right.
        """
        db = _FakeSession()
        _seed_valid_reset(db)

        with (
            patch.object(
                pr_module.token_service, "revoke_all_user_tokens_in_transaction", return_value=0
            ) as safe,
            patch.object(pr_module.token_service, "revoke_all_user_tokens") as committing,
            patch.object(pr_module, "check_password_against_history", return_value=True),
            patch.object(pr_module, "add_password_to_history"),
            patch.object(pr_module.settings, "PASSWORD_POLICY_ENABLED", False),
        ):
            pr_module.confirm_password_reset(cast(Session, db), "raw-token", "NewPassw0rd!x")

        assert safe.called, "must use the transaction-aware helper"
        assert not committing.called, "must not use the self-committing helper mid-transaction"


# ---------------------------------------------------------------------------
# Minimal fakes — this exercises transaction control, not SQL.
# ---------------------------------------------------------------------------


class _FakeUser:
    def __init__(self):
        self.id = 1
        self.email = "user@example.com"
        self.hashed_password = _OLD_HASH
        self.password_changed_at = None
        self.must_change_password = True


class _FakeToken:
    def __init__(self):
        self.id = 7
        self.user_id = 1
        self.used_at = None


class _FakeQuery:
    def __init__(self, session, result):
        self._session = session
        self._result = result

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._result

    def update(self, *_args, **_kwargs):
        return 0


class _FakeSession:
    """Just enough Session to drive confirm_password_reset's control flow."""

    def __init__(self):
        self.token_record = _FakeToken()
        self.user = _FakeUser()
        self.committed = False
        self.rolled_back = False
        self.on_commit = None
        self._next = "token"

    def query(self, model):
        # confirm_password_reset queries PasswordResetToken, then User, then
        # PasswordResetToken again for the bulk update.
        name = getattr(model, "__name__", str(model))
        if "User" in name:
            return _FakeQuery(self, self.user)
        return _FakeQuery(self, self.token_record)

    def commit(self):
        if self.on_commit:
            self.on_commit()
        self.committed = True

    def rollback(self):
        self.rolled_back = True
        # A real rollback discards the in-memory changes too.
        self.user.hashed_password = _OLD_HASH

    def add(self, _obj):
        pass

    def flush(self):
        pass


def _seed_valid_reset(db: _FakeSession) -> tuple[_FakeToken, _FakeUser]:
    return db.token_record, db.user
