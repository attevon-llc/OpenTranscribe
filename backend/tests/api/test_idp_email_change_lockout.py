"""An IdP-side email change must not be an unrecoverable lockout (issue #867).

``account_linking.assert_provider_id_link_permitted`` refuses a provider-ID-matched login
whose asserted email no longer agrees with the account's stored email. That check is correct
and stays: it is the one signal that the *person* behind a recycled ``sub`` / ``uid`` / DN has
changed, and removing it reopens the account-takeover vector the module exists to close.

What was wrong is that the refusal had **no way out**. The gate runs before
``_update_oidc_user`` — the only code that would write the IdP's new address onto the row — so
the stored email can never catch up, and every subsequent login re-runs the identical sequence
to the identical 401. The self-heal path was unreachable *by construction*, and no admin
endpoint wrote ``user.email`` either, so the only remedy was direct SQL.

Two changes, tested here:

1. **The comparison is normalised** (``.strip().lower()``, exactly as
   ``api/endpoints/auth/dependencies.py:_enforce_proxy_identity_consistency`` already compares
   an asserted address to a stored one). An IdP that re-cases an address is not asserting a
   different person, and locking an account out over it is indefensible. This is a narrowing of
   the *false-positive* surface, not a weakening: two different people never differ only by
   case.
2. **An explicit super_admin remedy** — ``PUT /api/admin/users/{uuid}/external-email`` —
   deliberately mirroring ``link-identity``: same tier, same audit shape, same principle that
   an administrator makes this decision and an external directory does not. Auto-accepting the
   new address was rejected: a recycled identifier asserting a *different* person's email is
   indistinguishable, at the point of the check, from a legitimate rename.

⚠️ The controls at the bottom are what keep the remedy narrow. Without them this endpoint is
"a super_admin can rewrite anyone's login email", which is a bigger authority than the bug
needs and is not what was built.
"""

from __future__ import annotations

import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import cast

import pytest
from fastapi import HTTPException
from fastapi import status

from app.auth.oidc.claims import OIDCUserData
from app.auth.oidc.provisioning import sync_oidc_user_to_db
from app.core.security import get_password_hash
from app.models.user import User

pytestmark = pytest.mark.xdist_group("idp_email_change_lockout")

_REMEDY = "/api/admin/users/{uuid}/external-email"


def _claims(subject: str, email: str, **overrides) -> OIDCUserData:
    """A minimal verified-claim set, exactly the ``OIDCUserData`` a verified ID token yields."""
    data: dict = {
        "oidc_subject": subject,
        "email": email,
        "email_verified": True,
        "full_name": "Alice Renamed",
        "username": subject,
        "is_admin": False,
        "roles": [],
        "claim_keys": [],
        "roles_claim_source": "none",
        "groups_overage": False,
        "groupless_provider": False,
        "cert_dn": None,
        "cert_serial": None,
        "cert_issuer": None,
        "cert_org": None,
        "cert_ou": None,
        "cert_valid_from": None,
        "cert_valid_until": None,
        "cert_fingerprint": None,
    }
    data.update(overrides)
    # A TypedDict cannot be built from a `**overrides` splat, and the keys above are
    # the complete OIDCUserData key set — mypy checks the annotation at every call site.
    return cast("OIDCUserData", data)


@pytest.fixture
def linked_user(db_session):
    """An account already JIT-linked to an OIDC subject, exactly as a first login leaves it."""
    tag = uuid_pkg.uuid4().hex[:8]
    user = User(
        email=f"alice-{tag}@example.com",
        full_name="Alice",
        hashed_password=get_password_hash("irrelevant-Passphrase99!"),
        role="user",
        auth_type="oidc",
        oidc_subject=f"sub-{uuid_pkg.uuid4().hex}",
        is_active=True,
        is_superuser=False,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


# --------------------------------------------------------------------------- #
# The defect: a refusal with no way out
# --------------------------------------------------------------------------- #


def test_an_idp_email_change_locks_the_account_out_permanently(db_session, linked_user):
    """Two consecutive logins, two identical 401s, and the stored email never moves.

    Retrying is the thing a real user does, and it is what makes this a *lockout* rather
    than a failed login. The second attempt is not redundant: it is the assertion that the
    self-heal the module docstring describes genuinely cannot run.
    """
    new_email = f"alice.renamed-{uuid_pkg.uuid4().hex[:8]}@example.com"
    claims = _claims(str(linked_user.oidc_subject), new_email)
    original_email = str(linked_user.email)

    for attempt in (1, 2):
        with pytest.raises(HTTPException) as excinfo:
            sync_oidc_user_to_db(db_session, claims)
        assert excinfo.value.status_code == status.HTTP_401_UNAUTHORIZED, (
            f"attempt {attempt} did not refuse"
        )

    db_session.refresh(linked_user)
    assert str(linked_user.email) == original_email, (
        "the stored email moved on its own — the guard is no longer refusing before the "
        "profile refresh, so this suite's premise is stale"
    )


def test_the_refusal_names_the_asserted_email_so_an_operator_can_decide(
    db_session, linked_user, monkeypatch
):
    """The audit record must carry the address the source asserted.

    ``provider_id_email_mismatch`` alone cannot be acted on: the operator's whole job here
    is to tell a legitimate rename ('alice@' -> 'alice.smith@') apart from a recycled
    identifier now belonging to someone else ('bob@'), and that judgement needs both
    addresses. Without this the only way to reach the decision is to go and ask the IdP.
    """
    from app.auth import account_linking

    captured: list[dict] = []
    monkeypatch.setattr(
        account_linking.audit_logger,
        "log",
        lambda **kwargs: captured.append(kwargs),
    )

    new_email = f"alice.renamed-{uuid_pkg.uuid4().hex[:8]}@example.com"
    with pytest.raises(HTTPException):
        sync_oidc_user_to_db(db_session, _claims(str(linked_user.oidc_subject), new_email))

    assert captured, "the refusal emitted no audit record at all"
    details = captured[-1]["details"]
    assert details["reason"] == "provider_id_email_mismatch"
    assert details["asserted_email"] == new_email
    assert captured[-1]["user_id"] == linked_user.id


# --------------------------------------------------------------------------- #
# Fix 1 — a case-only change is not a different person
# --------------------------------------------------------------------------- #


def test_a_case_only_change_is_not_a_mismatch(db_session, linked_user):
    """No admin action should be needed for `Alice@Example.com` vs `alice@example.com`."""
    recased = str(linked_user.email).upper()
    assert recased != str(linked_user.email)

    user = sync_oidc_user_to_db(db_session, _claims(str(linked_user.oidc_subject), recased))

    assert user is not None
    assert user.id == linked_user.id


def test_surrounding_whitespace_is_not_a_mismatch(db_session, linked_user):
    same_with_padding = f"  {linked_user.email}  "

    user = sync_oidc_user_to_db(
        db_session, _claims(str(linked_user.oidc_subject), same_with_padding)
    )

    assert user is not None
    assert user.id == linked_user.id


# --------------------------------------------------------------------------- #
# Fix 2 — the admin remedy, and that it actually unblocks the login
# --------------------------------------------------------------------------- #


def test_the_admin_remedy_restores_a_locked_out_login(
    client, super_admin_token_headers, db_session, linked_user
):
    """The full round trip: locked out, remedied, logging in again.

    This is the acceptance criterion of issue #867 — not that an endpoint exists, but that
    applying it makes the *next provider login* succeed through the ordinary path.
    """
    new_email = f"alice.renamed-{uuid_pkg.uuid4().hex[:8]}@example.com"
    claims = _claims(str(linked_user.oidc_subject), new_email)

    with pytest.raises(HTTPException):
        sync_oidc_user_to_db(db_session, claims)

    response = client.put(
        _REMEDY.format(uuid=linked_user.uuid),
        json={"email": new_email},
        headers=super_admin_token_headers,
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["email"] == new_email

    user = sync_oidc_user_to_db(db_session, claims)
    assert user is not None
    assert user.id == linked_user.id
    db_session.refresh(linked_user)
    assert str(linked_user.email) == new_email


def test_the_remedy_revokes_the_accounts_sessions(
    client, super_admin_token_headers, db_session, linked_user
):
    """The email is this account's login identifier; changing it is a credential-class
    change, and `auth/CLAUDE.md` makes revocation non-optional for those."""
    from app.models.refresh_token import RefreshToken

    token = RefreshToken(
        user_id=linked_user.id,
        token_hash=f"hash-{uuid_pkg.uuid4().hex}",
        jti=str(uuid_pkg.uuid4()),
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    db_session.add(token)
    db_session.commit()

    response = client.put(
        _REMEDY.format(uuid=linked_user.uuid),
        json={"email": f"alice.renamed-{uuid_pkg.uuid4().hex[:8]}@example.com"},
        headers=super_admin_token_headers,
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    db_session.refresh(token)
    assert token.revoked_at is not None, "the account's sessions survived an identity change"


def test_the_remedy_is_audited_naming_its_target(
    client, super_admin_token_headers, db_session, linked_user, monkeypatch
):
    """Issue #443's invariant: ``user_id`` is the ACTOR, the subject is a top-level target."""
    from app.api.endpoints import admin as admin_module

    captured: list[dict] = []
    monkeypatch.setattr(admin_module.audit_logger, "log", lambda **kwargs: captured.append(kwargs))

    new_email = f"alice.renamed-{uuid_pkg.uuid4().hex[:8]}@example.com"
    response = client.put(
        _REMEDY.format(uuid=linked_user.uuid),
        json={"email": new_email},
        headers=super_admin_token_headers,
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    assert captured, "the remedy emitted no audit record"
    event = captured[-1]
    assert event["target_user_id"] == linked_user.id
    assert event["user_id"] != linked_user.id, "user_id must be the acting admin, not the subject"
    assert event["details"]["action"] == "update_external_email"


# --------------------------------------------------------------------------- #
# Controls — the remedy must stay narrow, and the guard must stay a guard
# --------------------------------------------------------------------------- #


def test_a_genuinely_different_person_is_still_refused(db_session, linked_user):
    """The must-not-fire control for fix 1. A recycled ``sub`` asserting somebody else's
    address is the exact attack ``assert_provider_id_link_permitted`` exists to stop, and
    normalising case must not have opened it."""
    with pytest.raises(HTTPException) as excinfo:
        sync_oidc_user_to_db(
            db_session,
            _claims(str(linked_user.oidc_subject), f"mallory-{uuid_pkg.uuid4().hex[:8]}@evil.test"),
        )
    assert excinfo.value.status_code == status.HTTP_401_UNAUTHORIZED


def test_the_remedy_refuses_a_super_admin_target(
    client, super_admin_token_headers, db_session, super_admin_user
):
    """Matching ``link-identity``: a super_admin is local-only and is the break-glass
    account for exactly the IdP that might be failing."""
    response = client.put(
        _REMEDY.format(uuid=super_admin_user.uuid),
        json={"email": f"newsa-{uuid_pkg.uuid4().hex[:8]}@example.com"},
        headers=super_admin_token_headers,
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text


def test_the_remedy_refuses_an_account_with_no_external_identity(
    client, super_admin_token_headers, db_session, normal_user
):
    """This is a remedy for an IdP-linked account, NOT a general 'edit anyone's email'.

    Without this narrowing the endpoint would be a new, broader authority than the bug
    calls for: rewriting a local account's email rewrites the credential it logs in with.
    """
    response = client.put(
        _REMEDY.format(uuid=normal_user.uuid),
        json={"email": f"whatever-{uuid_pkg.uuid4().hex[:8]}@example.com"},
        headers=super_admin_token_headers,
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text


def test_the_remedy_refuses_an_email_another_account_already_holds(
    client, super_admin_token_headers, db_session, linked_user, normal_user
):
    """A 409, not a 500 on the unique index — and never a silent takeover of that account."""
    response = client.put(
        _REMEDY.format(uuid=linked_user.uuid),
        json={"email": str(normal_user.email)},
        headers=super_admin_token_headers,
    )
    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    db_session.refresh(linked_user)
    assert str(linked_user.email) != str(normal_user.email)


def test_the_conflict_check_is_case_insensitive(
    client, super_admin_token_headers, db_session, linked_user, normal_user
):
    """The unique index is byte-exact, but ``emails_agree`` is not — so two rows differing
    only by case would make "which account is this?" depend on which comparison ran."""
    response = client.put(
        _REMEDY.format(uuid=linked_user.uuid),
        json={"email": str(normal_user.email).upper()},
        headers=super_admin_token_headers,
    )
    assert response.status_code == status.HTTP_409_CONFLICT, response.text


def test_the_remedy_requires_super_admin(client, admin_token_headers, db_session, linked_user):
    """An ordinary admin manages users; changing how an identity resolves is deployment
    configuration, which `auth/CLAUDE.md` puts in the super_admin tier."""
    response = client.put(
        _REMEDY.format(uuid=linked_user.uuid),
        json={"email": f"nope-{uuid_pkg.uuid4().hex[:8]}@example.com"},
        headers=admin_token_headers,
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN, response.text


# --------------------------------------------------------------------------- #
# The fix is at the SHARED choke point, not in one provider
# --------------------------------------------------------------------------- #

#: Every method that reaches ``assert_provider_id_link_permitted``. The issue named
#: five call sites (LDAP, OIDC, SAML, PKI, the registry JIT seam); the OIDC tests
#: above drive one of them end to end, and these pin that the other four inherit the
#: same behaviour from the one guard rather than needing four bespoke fixes.
_PROVIDERS = ("ldap", "oidc", "saml", "pki", "external")


class _FakeLinkedUser:
    def __init__(self, email: str, role: str = "user") -> None:
        self.id = 1
        self.email = email
        self.role = role


@pytest.mark.parametrize("provider", _PROVIDERS)
def test_every_provider_inherits_the_normalised_comparison(provider):
    from app.auth.account_linking import assert_provider_id_link_permitted

    assert_provider_id_link_permitted(
        _FakeLinkedUser("alice@example.com"),
        provider=provider,
        source_identifier="identifier",
        asserted_email="  ALICE@Example.COM ",
        failure_detail="nope",
    )


@pytest.mark.parametrize("provider", _PROVIDERS)
def test_every_provider_is_unblocked_once_the_stored_email_is_updated(provider):
    """What the remedy endpoint does — write ``user.email`` — is what every provider's
    guard reads, so one endpoint remedies all five."""
    from app.auth.account_linking import assert_provider_id_link_permitted

    user = _FakeLinkedUser("alice@example.com")
    asserted = "alice.renamed@example.com"

    with pytest.raises(HTTPException):
        assert_provider_id_link_permitted(
            user,
            provider=provider,
            source_identifier="identifier",
            asserted_email=asserted,
            failure_detail="nope",
        )

    user.email = asserted  # exactly what PUT .../external-email persists

    assert_provider_id_link_permitted(
        user,
        provider=provider,
        source_identifier="identifier",
        asserted_email=asserted,
        failure_detail="nope",
    )


@pytest.mark.parametrize("provider", _PROVIDERS)
def test_no_provider_gains_a_super_admin_bypass_from_the_remedy(provider):
    """Must-not-fire control: a matching email has never been enough for a super_admin,
    and normalising the comparison must not have made it so."""
    from app.auth.account_linking import assert_provider_id_link_permitted

    with pytest.raises(HTTPException):
        assert_provider_id_link_permitted(
            _FakeLinkedUser("root@example.com", role="super_admin"),
            provider=provider,
            source_identifier="identifier",
            asserted_email="root@example.com",
            failure_detail="nope",
        )


def test_the_remedy_404s_for_an_unknown_user(client, super_admin_token_headers):
    """The detail is asserted, not just the status: a missing ROUTE is also a 404, so a
    status-only assertion here would pass against an endpoint that was never built."""
    response = client.put(
        _REMEDY.format(uuid=uuid_pkg.uuid4()),
        json={"email": "nobody@example.com"},
        headers=super_admin_token_headers,
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert response.json()["detail"] == "User not found"
