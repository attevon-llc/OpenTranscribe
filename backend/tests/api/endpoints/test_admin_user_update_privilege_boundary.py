"""``PUT /api/users/{uuid}`` must never be a fourth, unguarded email-change path (issue #867).

``auth/account_linking.py`` is the single implementation of "who may write a user's email",
and it named exactly two authorities: the account holder themselves (``PUT /users/me``,
password-proven) and a super_admin accepting an IdP's updated address for an already-linked
account (``PUT /admin/users/{uuid}/external-email``). ``PUT /api/users/{uuid}`` — the plain
admin-tier user-update route in ``users.py`` — was a third, silent writer: ANY admin caller
could repoint any account's login identity through it, with no password proof, and a
super_admin's write through it was not even routed through the deliberate #867 remedy. That is
the account-takeover shape ``account_linking`` exists to close on the other two paths, reopened
on this one.

Two independent boundaries this suite pins:

1. A caller who is not ``super_admin`` may not write to a ``super_admin`` account through this
   route AT ALL (not just the previously-known privileged fields).
2. This route never changes ``user.email``, for ANY caller, including ``super_admin`` —
   decision (a)/(b) from the #867 follow-up: uniform refusal, 403, no shared-helper exception.

Tests 1, 2, 3, 5, 6, 9 are RED before the fix and GREEN after. Tests 4, 7, 8, 10 are controls
that must pass on both sides — proving the fix does not over-refuse. Test 11 pins the audit
record's actor/subject shape (issue #443). Tests 12/13 (the SCIM analogues) live in
``test_scim.py`` beside its existing SCIM suite, not here.

Two extension tests at the bottom cover the same guard shape on
``POST /admin/users/{uuid}/lock`` and ``DELETE /admin/users/{uuid}/sessions`` — both were
admin-tier gated with no super_admin-target check, which let a plain admin lock, or
force-logout the sessions of, every super_admin account (issue #867 finding (e)).
"""

from __future__ import annotations

import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from app.core.security import get_password_hash
from app.models.refresh_token import RefreshToken
from app.models.user import User


def _make_super_admin(db_session, *, password: str = "irrelevant-Passphrase99!") -> User:
    """A second, independent super_admin row — for tests where the fixture-provided
    ``super_admin_user`` is the ACTOR and a different account must be the TARGET."""
    tag = uuid_pkg.uuid4().hex[:8]
    user = User(
        email=f"other-super-{tag}@example.com",
        full_name="Other Super Admin",
        hashed_password=get_password_hash(password),
        role="super_admin",
        auth_type="local",
        is_active=True,
        is_superuser=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _live_session(db_session, user: User) -> RefreshToken:
    token = RefreshToken(
        user_id=user.id,
        token_hash=f"hash-{uuid_pkg.uuid4().hex}",
        jti=str(uuid_pkg.uuid4()),
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    db_session.add(token)
    db_session.commit()
    db_session.refresh(token)
    return token


# --------------------------------------------------------------------------- #
# Boundary 2 — this route never changes user.email
# --------------------------------------------------------------------------- #


def test_1_plain_admin_cannot_change_another_users_email(
    client, admin_token_headers, normal_user, db_session
):
    """RED before the fix (200, email changed) / GREEN after (403, email unchanged)."""
    new_email = f"attacker-{uuid_pkg.uuid4().hex[:8]}@example.com"

    response = client.put(
        f"/api/users/{normal_user.uuid}",
        headers=admin_token_headers,
        json={"email": new_email},
    )

    assert response.status_code == 403, response.text
    db_session.refresh(normal_user)
    assert str(normal_user.email) != new_email


def test_2_super_admin_cannot_change_another_users_email_either(
    client, super_admin_token_headers, normal_user, db_session
):
    """Decision (a): the refusal is uniform across EVERY caller, including super_admin —
    there is no shared-helper exception carved out for the highest tier."""
    new_email = f"attacker-{uuid_pkg.uuid4().hex[:8]}@example.com"

    response = client.put(
        f"/api/users/{normal_user.uuid}",
        headers=super_admin_token_headers,
        json={"email": new_email},
    )

    assert response.status_code == 403, response.text
    db_session.refresh(normal_user)
    assert str(normal_user.email) != new_email


def test_5_email_change_request_is_refused_wholly_not_partially(
    client, admin_token_headers, normal_user, db_session
):
    """A request carrying BOTH an email change and an ordinary field must be refused as a
    unit — the guard runs before the update loop, so nothing in the same request is
    partially applied. Before the fix both fields wrote; after, neither does."""
    new_email = f"attacker-{uuid_pkg.uuid4().hex[:8]}@example.com"
    original_name = str(normal_user.full_name)

    response = client.put(
        f"/api/users/{normal_user.uuid}",
        headers=admin_token_headers,
        json={"full_name": "Renamed By Refused Request", "email": new_email},
    )

    assert response.status_code == 403, response.text
    db_session.refresh(normal_user)
    assert str(normal_user.email) != new_email
    assert str(normal_user.full_name) == original_name, (
        "full_name persisted even though the whole request was refused"
    )


def test_6_admin_cannot_change_their_own_email_through_this_route(
    client, admin_token_headers, admin_user, db_session
):
    """Self-targeting through the admin route is not an exception — ``PUT /users/me`` is
    the only self-service path, and it requires the current password this route never asks
    for."""
    new_email = f"admin-renamed-{uuid_pkg.uuid4().hex[:8]}@example.com"

    response = client.put(
        f"/api/users/{admin_user.uuid}",
        headers=admin_token_headers,
        json={"email": new_email},
    )

    assert response.status_code == 403, response.text
    db_session.refresh(admin_user)
    assert str(admin_user.email) != new_email


def test_7_resubmitting_the_current_email_is_a_no_op_not_a_refusal(
    client, admin_token_headers, normal_user, db_session
):
    """Control: the guard must fire on a genuine CHANGE, not on a form that echoes back the
    address it read (case/whitespace aside) — must pass both before and after the fix."""
    response = client.put(
        f"/api/users/{normal_user.uuid}",
        headers=admin_token_headers,
        json={
            "full_name": "Echoed Address Update",
            "email": f"  {str(normal_user.email).upper()}  ",
        },
    )

    assert response.status_code == 200, response.text
    db_session.refresh(normal_user)
    assert normal_user.full_name == "Echoed Address Update"


def test_8_admin_can_still_update_ordinary_fields_on_a_normal_target(
    client, admin_token_headers, normal_user, db_session
):
    """Control: the base admin-update path for a non-email, non-super_admin-target write is
    completely unaffected."""
    response = client.put(
        f"/api/users/{normal_user.uuid}",
        headers=admin_token_headers,
        json={"full_name": "Ordinary Rename"},
    )

    assert response.status_code == 200, response.text
    db_session.refresh(normal_user)
    assert normal_user.full_name == "Ordinary Rename"


# --------------------------------------------------------------------------- #
# Boundary 1 — a non-super_admin caller may not touch a super_admin target AT ALL
# --------------------------------------------------------------------------- #


def test_3_plain_admin_cannot_modify_a_super_admin_target_at_all(
    client, admin_token_headers, super_admin_user, db_session
):
    """Before the fix, only the FIVE privileged fields were stripped for a plain admin —
    an ordinary field like full_name passed straight through even against a super_admin
    target. RED before (200, changed) / GREEN after (403, unchanged)."""
    response = client.put(
        f"/api/users/{super_admin_user.uuid}",
        headers=admin_token_headers,
        json={"full_name": "Admin Overwrote A Super Admin"},
    )

    assert response.status_code == 403, response.text
    db_session.refresh(super_admin_user)
    assert super_admin_user.full_name != "Admin Overwrote A Super Admin"


def test_4_super_admin_can_still_modify_a_super_admin_target(
    client, super_admin_token_headers, db_session
):
    """Control for boundary 1: a super_admin acting on ANOTHER super_admin account must
    still work — must pass both before and after the fix."""
    other = _make_super_admin(db_session)

    response = client.put(
        f"/api/users/{other.uuid}",
        headers=super_admin_token_headers,
        json={"full_name": "Legitimately Updated"},
    )

    assert response.status_code == 200, response.text
    db_session.refresh(other)
    assert other.full_name == "Legitimately Updated"


# --------------------------------------------------------------------------- #
# Test 9 / 10 — the adversarial chain, and the control that the real chain still works
# --------------------------------------------------------------------------- #


def test_9_an_admin_cannot_redirect_a_password_reset_by_rewriting_the_target_email(
    client, admin_token_headers, normal_user, db_session, monkeypatch
):
    """The end-to-end takeover this guard exists to close: an admin repoints a victim's
    email to an address the admin controls, then requests a password reset for THAT
    address, expecting to receive the reset link instead of the real owner.

    Before the fix: the PUT succeeds, the victim's row now carries the attacker's address,
    and ``request_password_reset`` resolves a matching account and mails the reset link to
    the attacker.

    After the fix: the PUT is refused, the victim's email never moves, so a reset request
    for the attacker's address matches no account at all and no reset email is ever sent
    for it.
    """
    from app.auth import password_reset as password_reset_module

    attacker_email = f"attacker-{uuid_pkg.uuid4().hex[:8]}@example.com"
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        password_reset_module.email_service,
        "send_password_reset",
        lambda to_email, reset_url: sent.append((to_email, reset_url)),
    )

    response = client.put(
        f"/api/users/{normal_user.uuid}",
        headers=admin_token_headers,
        json={"email": attacker_email},
    )
    assert response.status_code == 403, response.text

    password_reset_module.request_password_reset(db_session, attacker_email, "127.0.0.1")

    assert sent == [], (
        f"a reset link was mailed to the attacker's address: {sent!r} — the email change "
        "that should have been refused took effect"
    )


def test_10_a_legitimate_email_change_still_completes_the_reset_chain(
    client, user_token_headers, normal_user, db_session, monkeypatch
):
    """Control: a REAL email change through the one authorized self-service path
    (``PUT /users/me``, password-proven) must still let a password reset reach the NEW
    address — proving the #867 follow-up fix is narrow and does not collaterally break the
    legitimate chain it shares code with. Untouched by this change's code path, so it must
    pass identically before and after."""
    from app.auth import password_reset as password_reset_module

    new_email = f"renamed-{uuid_pkg.uuid4().hex[:8]}@example.com"
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        password_reset_module.email_service,
        "send_password_reset",
        lambda to_email, reset_url: sent.append((to_email, reset_url)),
    )

    response = client.put(
        "/api/users/me",
        headers=user_token_headers,
        json={"email": new_email, "current_password": "password123"},
    )
    assert response.status_code == 200, response.text

    password_reset_module.request_password_reset(db_session, new_email, "127.0.0.1")

    assert len(sent) == 1, sent
    assert sent[0][0] == new_email


# --------------------------------------------------------------------------- #
# Test 11 — the refusal is audited, actor vs. subject (issue #443)
# --------------------------------------------------------------------------- #


def test_11_a_denied_email_change_is_audited_naming_actor_and_subject(
    client, admin_token_headers, admin_user, normal_user, db_session, monkeypatch
):
    from app.api.endpoints import users as users_module
    from app.auth.audit import AuditOutcome

    captured: list[dict] = []
    monkeypatch.setattr(users_module.audit_logger, "log", lambda **kwargs: captured.append(kwargs))

    new_email = f"attacker-{uuid_pkg.uuid4().hex[:8]}@example.com"
    response = client.put(
        f"/api/users/{normal_user.uuid}",
        headers=admin_token_headers,
        json={"email": new_email},
    )

    assert response.status_code == 403, response.text
    assert captured, "the refusal emitted no audit record at all"
    event = captured[-1]
    assert event["outcome"] == AuditOutcome.FAILURE
    assert event["user_id"] == admin_user.id, "user_id must be the ACTOR"
    assert event["target_user_id"] == normal_user.id, "target_user_id must be the SUBJECT"
    assert event["target_username"] == str(normal_user.email)
    assert event["details"]["action"] == "email_change_denied"


# --------------------------------------------------------------------------- #
# Extension (issue #867 finding e) — the same super_admin-target guard on
# lock / terminate-sessions
# --------------------------------------------------------------------------- #


def test_plain_admin_cannot_lock_a_super_admin(
    client, admin_token_headers, super_admin_user, db_session
):
    """RED before the fix (200, locked) / GREEN after (403, untouched)."""
    response = client.post(
        f"/api/admin/users/{super_admin_user.uuid}/lock",
        headers=admin_token_headers,
    )

    assert response.status_code == 403, response.text
    db_session.refresh(super_admin_user)
    assert super_admin_user.is_active is True


def test_super_admin_can_still_lock_a_super_admin(client, super_admin_token_headers, db_session):
    """Control: must pass both before and after the fix."""
    target = _make_super_admin(db_session)

    response = client.post(
        f"/api/admin/users/{target.uuid}/lock",
        headers=super_admin_token_headers,
    )

    assert response.status_code == 200, response.text
    db_session.refresh(target)
    assert target.is_active is False


def test_plain_admin_cannot_terminate_a_super_admins_sessions(
    client, admin_token_headers, super_admin_user, db_session
):
    """RED before the fix (200, session revoked) / GREEN after (403, session alive)."""
    token = _live_session(db_session, super_admin_user)

    response = client.delete(
        f"/api/admin/users/{super_admin_user.uuid}/sessions",
        headers=admin_token_headers,
    )

    assert response.status_code == 403, response.text
    db_session.refresh(token)
    assert token.revoked_at is None


def test_super_admin_can_still_terminate_a_super_admins_sessions(
    client, super_admin_token_headers, db_session
):
    """Control: must pass both before and after the fix."""
    target = _make_super_admin(db_session)
    token = _live_session(db_session, target)

    response = client.delete(
        f"/api/admin/users/{target.uuid}/sessions",
        headers=super_admin_token_headers,
    )

    assert response.status_code == 200, response.text
    db_session.refresh(token)
    assert token.revoked_at is not None
