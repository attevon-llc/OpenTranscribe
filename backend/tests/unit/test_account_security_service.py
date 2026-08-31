"""Real-invocation characterization tests for ``app/services/account_security_service.py``.

Every other test file in this suite reaches this module only as a ``patch()`` target —
grep the tree and every hit is ``mock.patch("app.services.account_security_service...")``.
None of them ever call the real functions, so none of them can catch a regression in the
module ``app/services/CLAUDE.md`` calls "the single implementation" of three rules every
credential- or privilege-changing endpoint depends on: the password policy, session
revocation, and the audit trail. This file calls the real functions, against a real
``db_session`` and real ``User``/``RefreshToken`` rows, and asserts on real state.

Following the characterization-test convention of ``tests/unit/test_transcription_storage.py``.

What is pinned here, in order:

1. **``revoke_all_sessions`` is fail-open.** A raising ``token_service`` call must not
   propagate — it must return ``0`` — and must leave real token rows untouched. The
   success path is pinned separately: a real call against real ``RefreshToken`` rows
   really revokes them (DB ``revoked_at`` set, Redis-shaped blacklist entries written)
   and returns the true count.
2. **The epoch's ``<`` boundary, not ``<=``.** ``reissue_current_session``'s own
   docstring calls this out: "the epoch check compares with ``<``, so a token minted in
   the same second as the epoch survives by design." Pinned two ways — end to end (a
   session ``reissue_current_session`` actually mints right after
   ``revoke_all_sessions`` stamps the epoch must not be considered revoked), and at the
   exact boundary (a token timestamped to the real, just-stamped epoch survives; one
   second earlier does not). The boundary case uses the REAL epoch a real
   ``revoke_all_sessions`` call stamped — not a fabricated one — so it is the actual
   production comparison, not a look-alike. Every test here uses a private
   ``TokenService`` on an ``InMemoryStore`` (the pattern established by
   ``tests/unit/test_access_token_revocation_epoch.py``), so none of this depends on a
   live Redis and none of it can pollute — or be polluted by — the shared singleton.
3. **``audit_password_change``'s second event is actor-conditional.** The
   ``ADMIN_USER_UPDATE`` event fires only when ``actor.id != user.id`` (an admin reset);
   a self-service change only logs ``log_password_change``. Both branches are exercised
   with real ``User`` rows and a spied ``audit_logger``.
4. **``notify_email_changed`` swallows every exception from ``email_service``.** A
   forced delivery failure must not propagate — the docstring is explicit that delivery
   failure must never block the change it describes.
5. **``assert_local_fallback_settable`` / ``assert_password_auth_possible``** — both
   call the single-implementation predicates in ``app/auth/utils.py``
   (``local_fallback_permitted_for`` / ``local_password_allowed``). Both predicates are
   pure functions of their arguments, so real ``auth_type`` values exercise both the
   allow and the deny branch without mocking anything — a stronger check than patching
   the predicate, since it also proves this module calls the predicate it imports.
"""

from __future__ import annotations

from http.cookies import SimpleCookie
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from fastapi import Response

from app.auth.audit import AuditEventType
from app.auth.session import InMemoryStore
from app.auth.token_service import USER_REVOCATION_EPOCH_PREFIX
from app.auth.token_service import TokenService
from app.core.config import settings
from app.core.security import verify_token
from app.models.refresh_token import RefreshToken
from app.services import account_security_service
from tests.helpers import fake_request


@pytest.fixture
def isolated_token_service(monkeypatch):
    """A private ``TokenService`` on an ``InMemoryStore``, wired into the module under test.

    Same shape as ``tests/unit/test_access_token_revocation_epoch.py``'s ``revocation``
    fixture: no live Redis required, and no risk of leaking blacklist/epoch state into
    (or out of) the shared module-level ``token_service`` singleton other tests use.
    """
    service = TokenService()
    service._store = InMemoryStore()
    monkeypatch.setattr(account_security_service, "token_service", service)
    monkeypatch.setattr(settings, "TOKEN_REVOCATION_ENABLED", True)
    return service


def _mint_refresh_token(service: TokenService, db, user) -> RefreshToken:
    _token, row = service.create_refresh_token(
        db=db, user_id=user.id, user_uuid=str(user.uuid), role=str(user.role)
    )
    return row


def _cookie_value(response: Response, name: str) -> str:
    cookie: SimpleCookie = SimpleCookie()
    for raw in response.headers.getlist("set-cookie"):
        cookie.load(raw)
    return cookie[name].value


# ── revoke_all_sessions ──────────────────────────────────────────────────────────────


def test_revoke_all_sessions_revokes_real_tokens_and_returns_the_actual_count(
    db_session, normal_user, isolated_token_service
):
    row1 = _mint_refresh_token(isolated_token_service, db_session, normal_user)
    row2 = _mint_refresh_token(isolated_token_service, db_session, normal_user)

    revoked_count = account_security_service.revoke_all_sessions(
        db_session, normal_user, reason="password_change"
    )

    assert revoked_count == 2

    persisted = (
        db_session.query(RefreshToken)
        .filter(RefreshToken.user_id == normal_user.id)
        .order_by(RefreshToken.id)
        .all()
    )
    assert len(persisted) == 2
    assert all(row.revoked_at is not None for row in persisted)

    # The stateless-access-token side of revocation rides the same blacklist keys.
    assert isolated_token_service.store.get(f"revoked:jti:{row1.jti}") is not None
    assert isolated_token_service.store.get(f"revoked:jti:{row2.jti}") is not None


def test_revoke_all_sessions_swallows_the_underlying_exception_and_returns_zero(
    db_session, normal_user, isolated_token_service, monkeypatch
):
    row = _mint_refresh_token(isolated_token_service, db_session, normal_user)
    monkeypatch.setattr(
        isolated_token_service,
        "revoke_all_user_tokens_in_transaction",
        Mock(side_effect=RuntimeError("redis exploded")),
    )

    revoked_count = account_security_service.revoke_all_sessions(
        db_session, normal_user, reason="password_change"
    )

    assert revoked_count == 0

    # Fail-open means a clean no-op, not a partial mutation: the real row is untouched.
    persisted = db_session.query(RefreshToken).filter(RefreshToken.id == row.id).one()
    assert persisted.revoked_at is None


# ── reissue_current_session: the epoch's `<` boundary ───────────────────────────────


def test_reissue_current_session_survives_the_epoch_it_was_just_issued_under(
    db_session, normal_user, isolated_token_service
):
    """End to end: revoke, then reissue — the new session must not kill itself."""
    account_security_service.revoke_all_sessions(
        db_session, normal_user, reason="self_service_password_change"
    )
    epoch = int(
        isolated_token_service.store.get(f"{USER_REVOCATION_EPOCH_PREFIX}{normal_user.uuid}")
    )

    response = Response()
    account_security_service.reissue_current_session(
        db_session,
        normal_user,
        response,
        fake_request(),
        user_agent="pytest",
        ip_address="127.0.0.1",
    )

    access_token = _cookie_value(response, "access_token")
    claims = verify_token(access_token)

    # A session reissued after revocation can never predate the epoch it followed.
    assert claims["iat"] >= epoch
    assert (
        isolated_token_service.is_token_revoked(
            claims["jti"],
            db=db_session,
            user_uuid=str(normal_user.uuid),
            issued_at=claims["iat"],
        )
        is False
    )


def test_the_epoch_check_uses_strict_less_than_not_less_than_or_equal(
    db_session, normal_user, isolated_token_service
):
    """Pins the exact off-by-one the docstring warns about, against the REAL epoch a
    real ``revoke_all_sessions`` call stamped -- not a fabricated timestamp."""
    account_security_service.revoke_all_sessions(db_session, normal_user, reason="test")
    epoch = int(
        isolated_token_service.store.get(f"{USER_REVOCATION_EPOCH_PREFIX}{normal_user.uuid}")
    )

    # Minted in the SAME SECOND as the epoch: survives by design (`<`, not `<=`).
    assert (
        isolated_token_service.is_token_revoked(
            "same-second-jti", db=db_session, user_uuid=str(normal_user.uuid), issued_at=epoch
        )
        is False
    )
    # One second earlier: revoked. Proves the check discriminates rather than
    # vacuously returning False for everything.
    assert (
        isolated_token_service.is_token_revoked(
            "one-second-earlier-jti",
            db=db_session,
            user_uuid=str(normal_user.uuid),
            issued_at=epoch - 1,
        )
        is True
    )


# ── audit_password_change: the admin-driven second event ───────────────────────────


def test_self_service_password_change_omits_the_admin_event(monkeypatch, normal_user):
    log_calls: list[dict] = []
    log_password_change_calls: list[dict] = []
    monkeypatch.setattr(
        account_security_service.audit_logger, "log", lambda **kw: log_calls.append(kw)
    )
    monkeypatch.setattr(
        account_security_service.audit_logger,
        "log_password_change",
        lambda **kw: log_password_change_calls.append(kw),
    )

    account_security_service.audit_password_change(
        user=normal_user,
        actor=normal_user,
        client_ip="10.0.0.1",
        user_agent="pytest",
        forced=False,
    )

    assert len(log_password_change_calls) == 1
    assert log_password_change_calls[0]["user_id"] == normal_user.id
    assert log_calls == []


def test_admin_password_reset_also_emits_the_admin_user_update_event(
    monkeypatch, admin_user, normal_user
):
    log_calls: list[dict] = []
    log_password_change_calls: list[dict] = []
    monkeypatch.setattr(
        account_security_service.audit_logger, "log", lambda **kw: log_calls.append(kw)
    )
    monkeypatch.setattr(
        account_security_service.audit_logger,
        "log_password_change",
        lambda **kw: log_password_change_calls.append(kw),
    )

    account_security_service.audit_password_change(
        user=normal_user,
        actor=admin_user,
        client_ip="10.0.0.1",
        user_agent="pytest",
        forced=True,
    )

    assert len(log_password_change_calls) == 1
    assert log_password_change_calls[0]["forced"] is True

    assert len(log_calls) == 1
    assert log_calls[0]["event_type"] == AuditEventType.ADMIN_USER_UPDATE
    assert log_calls[0]["user_id"] == admin_user.id
    assert log_calls[0]["details"]["target_user"] == str(normal_user.uuid)


# ── notify_email_changed: swallows delivery failure ─────────────────────────────────


def test_notify_email_changed_swallows_email_service_failures(monkeypatch):
    import app.services.email_service as email_service_module

    send_mock = Mock(side_effect=RuntimeError("smtp connection refused"))
    monkeypatch.setattr(email_service_module.email_service, "send_security_notice", send_mock)

    try:
        account_security_service.notify_email_changed("old@example.com", "new@example.com")
    except Exception as exc:
        pytest.fail(f"notify_email_changed propagated {exc!r} instead of swallowing it")

    send_mock.assert_called_once_with(
        "old@example.com",
        "Your account email address was changed",
        "The email address on your account was changed to new@example.com.",
    )


# ── assert_password_auth_possible / assert_local_fallback_settable ─────────────────


def test_assert_password_auth_possible_allows_local_denies_ldap(normal_user):
    normal_user.auth_type = "local"
    # Returns None; a local account passes silently (no raise).
    account_security_service.assert_password_auth_possible(normal_user)

    normal_user.auth_type = "ldap"
    with pytest.raises(HTTPException) as exc:
        account_security_service.assert_password_auth_possible(normal_user)
    assert exc.value.status_code == 400
    assert "cannot set a password" in exc.value.detail.lower()


def test_assert_local_fallback_settable_allows_oidc_denies_local():
    # Returns None; oidc supports the fallback flag (no raise).
    account_security_service.assert_local_fallback_settable("oidc")

    with pytest.raises(HTTPException) as exc:
        account_security_service.assert_local_fallback_settable("local")
    assert exc.value.status_code == 400
