"""``_enforce_proxy_identity_consistency`` — the three narrowings, both directions.

Trusted-header authentication reads the header once, at sign-in, and then relies on
an ordinary session. Signing out of the upstream IdP and back in as somebody else
would otherwise leave the previous app session live (Open WebUI #14406), so
``get_current_active_user`` re-checks the assertion on every request and **revokes**
on a mismatch rather than merely refusing.

A control that revokes sessions is a denial-of-service the moment any of its three
narrowings is wrong, so each one is exercised in **both** directions here:

============================  ==============================  ============================
narrowing                     the allowed case                the denied case
============================  ==============================  ============================
only ``auth_type='proxy'``    a local/super_admin session      a proxy session
                              survives a header it never
                              used
only a **trusted** peer       an off-allowlist peer cannot     the configured CIDR revokes
                              revoke anybody
absence is not an assertion   no header → nothing happens      a present, different
                                                               address revokes
============================  ==============================  ============================

``tests/api/test_proxy_auth_endpoint.py::TestPerRequestConsistency`` already drives
the happy path and the "different identity revokes" case through the real router.
This suite is the unit-level complement and deliberately covers what that one
cannot reach: the ``auth_type`` narrowing (no route can present a *local* session
alongside a proxy header there, because the fixture logs in through the proxy
endpoint), ``request=None``, ``proxy_enabled=False``, a routable-but-untrusted peer
(TestClient's peer is the string ``"testclient"``, which is not an address at all),
the fail-closed empty allowlist, and the audit record's contents.

Everything is real: real ``starlette.requests.Request`` objects with real socket
peers and real headers, the real ``header_trust`` CIDR parser, real ``User`` and
``RefreshToken`` rows, and the real ``revoke_all_sessions``. Only ``audit_logger.log``
is intercepted, and only to read what it was given.
"""

# mypy: disable-error-code="arg-type,index"
# ``HTTPException.detail`` is typed ``str`` while every lifecycle gate raises an
# object detail, and the ``db``/``user`` parameters are declared ``Session``/``User``
# for the production call sites. Declared once here rather than as a cast per
# assertion — a cast at every call site buries the thing being asserted.
from __future__ import annotations

import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.endpoints.auth import dependencies as deps_module
from app.api.endpoints.auth.dependencies import ERROR_CODE_PROXY_IDENTITY_MISMATCH
from app.api.endpoints.auth.dependencies import get_current_active_user
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.core.config import settings
from app.core.security import get_password_hash
from app.models.refresh_token import RefreshToken
from app.models.user import User

_enforce = deps_module._enforce_proxy_identity_consistency

#: The allowlist an operator would configure for a proxy on the container network.
TRUSTED_CIDR = "10.0.0.0/8"
#: Inside it — the authenticating proxy.
TRUSTED_PEER = "10.0.0.7"
#: RFC 5737 TEST-NET-3, deliberately outside ``TRUSTED_CIDR``. A *routable* address
#: rather than a non-address: an untrusted peer must be refused because it is not on
#: the allowlist, not because its address failed to parse.
UNTRUSTED_PEER = "203.0.113.9"
#: Non-default on purpose, so a test passes only if the configured header is read.
EMAIL_HEADER = "X-Forwarded-Email"

ORDINARY_PATH = f"{settings.API_PREFIX}/files"
OTHER_IDENTITY = "someone.else@example.com"


# ── the deployment, and the requests that arrive at it ───────────────────────────


def _publish(**values: Any) -> None:
    """Stand a proxy deployment up without a database.

    ``_enforce_proxy_identity_consistency`` reads its configuration through
    ``get_process_auth_settings()`` (DB ``auth_config`` > ``.env`` > coded default)
    because it runs on every authenticated request and holds no query budget.
    Publishing the effective value is the supported way to configure that in a
    test; the autouse ``_clear_process_auth_cache`` fixture in ``tests/conftest.py``
    tears it down again.
    """
    from app.core.auth_settings import publish_process_auth_setting

    for key, value in values.items():
        publish_process_auth_setting(key, value)


@pytest.fixture
def proxy_enabled() -> None:
    """A deployment with trusted-header auth on and one trusted proxy network."""
    _publish(
        proxy_enabled=True,
        proxy_trusted_proxies=TRUSTED_CIDR,
        proxy_email_header=EMAIL_HEADER,
    )


@pytest.fixture
def audited(monkeypatch) -> list[dict]:
    """Capture what the gate hands to the audit logger.

    NOTE this patches ``log`` on the audit_logger INSTANCE, which is a shared singleton —
    so it captures EVERY emitter reached during the call, not only this module's.
    """
    events: list[dict] = []
    monkeypatch.setattr(deps_module.audit_logger, "log", lambda **kw: events.append(kw))
    return events


def _record(audited: list[dict], event_type: AuditEventType) -> dict:
    """The one record of *event_type*, selected by TYPE rather than by position.

    These assertions used to index ``audited[0]``. That broke the moment
    ``revoke_all_user_tokens_in_transaction`` gained its own ``AUTH_TOKEN_REVOKE`` record —
    which is a fix, not a regression: a mass revocation that emitted nothing was itself a
    finding. But it fires FIRST, so every ``audited[0]`` silently retargeted onto it and
    the suite failed with ``KeyError: 'source_ip'``.

    Positional indexing into an emitted-event list has the same defect as a positional
    ``.nth()`` DOM selector: it silently means something different as soon as anything is
    inserted above it. Assert on identity instead.
    """
    matches = [e for e in audited if e["event_type"] is event_type]
    assert len(matches) == 1, (
        f"expected exactly one {event_type} record, got {[e['event_type'] for e in audited]}"
    )
    return matches[0]


def _request(
    *,
    peer: str | None = TRUSTED_PEER,
    asserted: str | None = None,
    header: str = EMAIL_HEADER,
    path: str = ORDINARY_PATH,
    matched_route: bool = True,
) -> Request:
    """A real ``Request`` with a real socket peer and real headers.

    Args:
        peer: Socket peer address, or ``None`` for a transport that exposes none.
        asserted: Value for the proxy's identity header; ``None`` omits the header
            entirely, which is the "absence is not an assertion" case.
        header: Which header to send it in — a wrong name must not be honoured.
        path: Request path.
        matched_route: Whether a route has been resolved yet, as Starlette would
            have done by the time a dependency runs.
    """
    headers: list[tuple[bytes, bytes]] = [(b"user-agent", b"pytest")]
    if asserted is not None:
        headers.append((header.lower().encode(), asserted.encode()))

    scope: dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "server": ("backend", 8080),
        "root_path": "",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": (peer, 40000) if peer is not None else None,
    }
    if matched_route:
        scope["route"] = SimpleNamespace(path=path)
    return Request(scope)


# ── the accounts ────────────────────────────────────────────────────────────────


def _make_user(db_session, *, auth_type: str, role: str = "user", sessions: int = 2) -> User:
    """A real account with *sessions* live ``refresh_token`` rows (i.e. sessions)."""
    unique = uuid_pkg.uuid4().hex[:8]
    user = User(
        email=f"consistency-{unique}@example.com",
        full_name="Proxy Person",
        hashed_password=get_password_hash("irrelevant-Passphrase99!"),
        role=role,
        auth_type=auth_type,
        is_active=True,
        is_superuser=role == "super_admin",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    now = datetime.now(UTC)
    for index in range(sessions):
        db_session.add(
            RefreshToken(
                user_id=user.id,
                token_hash=f"hash-{unique}-{index}",
                jti=str(uuid_pkg.uuid4()),
                expires_at=now + timedelta(days=7),
            )
        )
    db_session.commit()
    return user


@pytest.fixture
def proxy_user(db_session) -> User:
    return _make_user(db_session, auth_type="proxy")


def _live_sessions(db_session, user: User) -> int:
    return int(
        db_session.query(RefreshToken)
        .filter(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .count()
    )


# ── narrowing 1: only accounts whose auth_type is 'proxy' ────────────────────────


class TestOnlyProxyAccountsAreSubjectToTheCheck:
    """A session that never used the header must not be terminated by one.

    This is the narrowing with real teeth: without it, a header any reachable
    trusted proxy can set would log out the local break-glass ``super_admin`` —
    exactly the account an operator needs when the IdP is misconfigured.
    """

    @pytest.mark.parametrize("auth_type", ["local", "ldap", "oidc", "pki", "saml"])
    def test_a_non_proxy_session_survives_a_mismatching_assertion(
        self, db_session, proxy_enabled, audited, auth_type
    ):
        user = _make_user(db_session, auth_type=auth_type)

        _enforce(user, _request(asserted=OTHER_IDENTITY), db_session)

        assert _live_sessions(db_session, user) == 2
        assert audited == []

    def test_the_break_glass_super_admin_is_never_logged_out_by_a_header(
        self, db_session, proxy_enabled, audited
    ):
        user = _make_user(db_session, auth_type="local", role="super_admin")

        _enforce(user, _request(asserted=OTHER_IDENTITY), db_session)

        assert _live_sessions(db_session, user) == 2
        assert audited == []

    def test_a_proxy_session_is_subject_to_it(self, db_session, proxy_enabled, audited, proxy_user):
        """The control's positive control: same request, only auth_type differs."""
        with pytest.raises(HTTPException) as exc:
            _enforce(proxy_user, _request(asserted=OTHER_IDENTITY), db_session)

        assert exc.value.status_code == 401
        assert _live_sessions(db_session, proxy_user) == 0

    def test_no_request_object_short_circuits(self, db_session, proxy_enabled, audited, proxy_user):
        """``request=None`` (a stand-in, or a non-HTTP caller) asserts nothing.

        The guard is an ``or``: either no request **or** a non-proxy account exits.
        Reading the headers off ``None`` would be an AttributeError, and the 500 it
        produced would be indistinguishable from a broken database.
        """
        _enforce(proxy_user, None, db_session)

        assert _live_sessions(db_session, proxy_user) == 2
        assert audited == []


# ── narrowing 2: only a header delivered by a trusted peer ───────────────────────


class TestOnlyATrustedPeerCanAssertAnIdentity:
    """Honouring an untrusted header would make this a DoS anyone can trigger."""

    def test_a_trusted_peer_revokes(self, db_session, proxy_enabled, audited, proxy_user):
        with pytest.raises(HTTPException) as exc:
            _enforce(proxy_user, _request(peer=TRUSTED_PEER, asserted=OTHER_IDENTITY), db_session)

        assert exc.value.status_code == 401
        assert _live_sessions(db_session, proxy_user) == 0

    def test_an_off_allowlist_peer_cannot_revoke_anyone(
        self, db_session, proxy_enabled, audited, proxy_user
    ):
        """Same account, same header, same mismatch — only the peer differs."""
        _enforce(proxy_user, _request(peer=UNTRUSTED_PEER, asserted=OTHER_IDENTITY), db_session)

        assert _live_sessions(db_session, proxy_user) == 2
        assert audited == []

    def test_a_transport_with_no_peer_cannot_revoke_anyone(
        self, db_session, proxy_enabled, audited, proxy_user
    ):
        """No socket peer → ``header_trust.UNKNOWN_PEER``, which is in no network."""
        _enforce(proxy_user, _request(peer=None, asserted=OTHER_IDENTITY), db_session)

        assert _live_sessions(db_session, proxy_user) == 2
        assert audited == []

    def test_an_empty_allowlist_trusts_nobody(self, db_session, audited, proxy_user):
        """Fail-closed: with no configured proxy, no header can revoke a session.

        The same rule ``auth/header_trust`` applies at sign-in — not "trust
        everyone", not "warn and continue".
        """
        _publish(proxy_enabled=True, proxy_trusted_proxies="", proxy_email_header=EMAIL_HEADER)

        _enforce(proxy_user, _request(asserted=OTHER_IDENTITY), db_session)

        assert _live_sessions(db_session, proxy_user) == 2
        assert audited == []

    def test_a_peer_in_a_different_configured_network_is_still_trusted(
        self, db_session, audited, proxy_user
    ):
        """The allowlist is a list: a second entry must be honoured too."""
        _publish(
            proxy_enabled=True,
            proxy_trusted_proxies=f"192.0.2.0/24, {TRUSTED_CIDR}",
            proxy_email_header=EMAIL_HEADER,
        )

        with pytest.raises(HTTPException) as exc:
            _enforce(proxy_user, _request(peer="192.0.2.44", asserted=OTHER_IDENTITY), db_session)

        assert exc.value.status_code == 401
        assert _live_sessions(db_session, proxy_user) == 0


# ── narrowing 3: absence is not an assertion ─────────────────────────────────────


class TestAbsenceIsNotAnAssertion:
    def test_no_header_at_all_leaves_the_session_alone(
        self, db_session, proxy_enabled, audited, proxy_user
    ):
        """A request that did not traverse the proxy carries no claim to compare."""
        _enforce(proxy_user, _request(asserted=None), db_session)

        assert _live_sessions(db_session, proxy_user) == 2
        assert audited == []

    def test_an_empty_header_value_is_not_an_assertion_either(
        self, db_session, proxy_enabled, audited, proxy_user
    ):
        _enforce(proxy_user, _request(asserted=""), db_session)

        assert _live_sessions(db_session, proxy_user) == 2
        assert audited == []

    def test_the_configured_header_name_is_the_one_read(
        self, db_session, proxy_enabled, audited, proxy_user
    ):
        """A different identity in the WRONG header is not an assertion at all."""
        _enforce(
            proxy_user,
            _request(asserted=OTHER_IDENTITY, header="X-Some-Other-Email"),
            db_session,
        )

        assert _live_sessions(db_session, proxy_user) == 2
        assert audited == []


# ── the switch, and the comparison itself ────────────────────────────────────────


class TestTheFeatureSwitchGatesTheCheck:
    def test_proxy_auth_disabled_enforces_nothing(self, db_session, audited, proxy_user):
        """An operator who turned the method off has no proxy to be consistent with."""
        _publish(
            proxy_enabled=False,
            proxy_trusted_proxies=TRUSTED_CIDR,
            proxy_email_header=EMAIL_HEADER,
        )

        _enforce(proxy_user, _request(asserted=OTHER_IDENTITY), db_session)

        assert _live_sessions(db_session, proxy_user) == 2
        assert audited == []

    def test_proxy_auth_enabled_enforces_it(self, db_session, audited, proxy_user):
        """Positive control for the line above: only ``proxy_enabled`` differs."""
        _publish(
            proxy_enabled=True,
            proxy_trusted_proxies=TRUSTED_CIDR,
            proxy_email_header=EMAIL_HEADER,
        )

        with pytest.raises(HTTPException) as exc:
            _enforce(proxy_user, _request(asserted=OTHER_IDENTITY), db_session)

        assert exc.value.status_code == 401
        assert _live_sessions(db_session, proxy_user) == 0


class TestTheIdentityComparison:
    """The address is the identity here, so the compare must be an address compare."""

    def test_the_same_address_passes_through(self, db_session, proxy_enabled, audited, proxy_user):
        _enforce(proxy_user, _request(asserted=str(proxy_user.email)), db_session)

        assert _live_sessions(db_session, proxy_user) == 2
        assert audited == []

    def test_case_differences_are_not_a_different_person(
        self, db_session, proxy_enabled, audited, proxy_user
    ):
        """An IdP that upper-cases the local part must not sign the user out."""
        _enforce(proxy_user, _request(asserted=str(proxy_user.email).upper()), db_session)

        assert _live_sessions(db_session, proxy_user) == 2
        assert audited == []

    def test_surrounding_whitespace_is_not_a_different_person(
        self, db_session, proxy_enabled, audited, proxy_user
    ):
        _enforce(proxy_user, _request(asserted=f"  {proxy_user.email}  "), db_session)

        assert _live_sessions(db_session, proxy_user) == 2
        assert audited == []

    def test_a_different_local_part_at_the_same_domain_is_a_mismatch(
        self, db_session, proxy_enabled, audited, proxy_user
    ):
        """Not a prefix/suffix compare: a neighbouring address is somebody else."""
        other = f"not-{proxy_user.email}"

        with pytest.raises(HTTPException) as exc:
            _enforce(proxy_user, _request(asserted=other), db_session)

        assert exc.value.status_code == 401
        assert _live_sessions(db_session, proxy_user) == 0


# ── what a mismatch actually does ────────────────────────────────────────────────


class TestAMismatchRevokesAndReports:
    """Revocation, not a bare 401: the session now belongs to nobody.

    Leaving the refresh token rotating would let the previous identity keep
    renewing a session it no longer owns.
    """

    def _mismatch(self, db_session, user: User) -> HTTPException:
        with pytest.raises(HTTPException) as exc:
            _enforce(user, _request(asserted=OTHER_IDENTITY), db_session)
        return exc.value

    def test_the_status_is_401(self, db_session, proxy_enabled, audited, proxy_user):
        assert self._mismatch(db_session, proxy_user).status_code == 401

    def test_the_detail_carries_the_machine_readable_code(
        self, db_session, proxy_enabled, audited, proxy_user
    ):
        """The SPA branches on ``detail.code``; the English prose is not a contract."""
        detail = self._mismatch(db_session, proxy_user).detail

        assert detail["code"] == ERROR_CODE_PROXY_IDENTITY_MISMATCH
        assert detail["message"]

    def test_every_session_is_revoked(self, db_session, proxy_enabled, audited, proxy_user):
        assert _live_sessions(db_session, proxy_user) == 2  # both live before the check

        self._mismatch(db_session, proxy_user)

        assert _live_sessions(db_session, proxy_user) == 0

    def test_the_revocation_is_committed(self, db_session, proxy_enabled, audited, proxy_user):
        """The request ends in an exception, so an uncommitted revocation is no
        revocation at all — the session would survive the very check that killed it."""
        self._mismatch(db_session, proxy_user)
        db_session.expire_all()

        assert _live_sessions(db_session, proxy_user) == 0

    def test_the_audit_event_is_a_session_termination(
        self, db_session, proxy_enabled, audited, proxy_user
    ):
        self._mismatch(db_session, proxy_user)

        # The mismatch record must be present. The list is NOT pinned to exactly one
        # entry: revoking the sessions legitimately emits its own AUTH_TOKEN_REVOKE, and
        # a mass revocation that recorded nothing was itself an audit finding. Requiring
        # a single record would mean this test fails whenever the revocation plane gets
        # MORE observable, which is backwards.
        assert AuditEventType.AUTH_SESSION_TERMINATED in [e["event_type"] for e in audited]
        terminated = _record(audited, AuditEventType.AUTH_SESSION_TERMINATED)
        assert terminated["error_code"] == "PROXY_IDENTITY_MISMATCH"
        # Recorded as a FAILURE, which is what makes it findable: an operator
        # reviewing the audit index filters on the outcome, and a denial logged
        # without one is a denial nobody sees.
        assert terminated["outcome"] is AuditOutcome.FAILURE

    def test_the_audit_record_names_the_account_it_terminated(
        self, db_session, proxy_enabled, audited, proxy_user
    ):
        """An operator reading the log must be able to tell WHOSE sessions went.

        These fields are the whole value of the record: without them it says only
        that *something* was revoked somewhere.
        """
        self._mismatch(db_session, proxy_user)

        assert _record(audited, AuditEventType.AUTH_SESSION_TERMINATED)["user_id"] == proxy_user.id
        assert _record(audited, AuditEventType.AUTH_SESSION_TERMINATED)["username"] == str(
            proxy_user.email
        )

    def test_the_audit_record_attributes_the_request(
        self, db_session, proxy_enabled, audited, proxy_user
    ):
        self._mismatch(db_session, proxy_user)

        assert _record(audited, AuditEventType.AUTH_SESSION_TERMINATED)["source_ip"] == TRUSTED_PEER
        assert _record(audited, AuditEventType.AUTH_SESSION_TERMINATED)["user_agent"] == "pytest"

    def test_the_audit_record_says_what_was_asserted_and_how_much_was_revoked(
        self, db_session, proxy_enabled, audited, proxy_user
    ):
        self._mismatch(db_session, proxy_user)
        details = _record(audited, AuditEventType.AUTH_SESSION_TERMINATED)["details"]

        assert details["asserted_identity"] == OTHER_IDENTITY
        assert details["sessions_revoked"] == 2
        assert details["path"] == ORDINARY_PATH

    def test_the_asserted_identity_is_recorded_normalised(
        self, db_session, proxy_enabled, audited, proxy_user
    ):
        """Recorded the way it was compared, so the record explains the decision."""
        with pytest.raises(HTTPException):
            _enforce(proxy_user, _request(asserted=f"  {OTHER_IDENTITY.upper()}  "), db_session)

        assert (
            _record(audited, AuditEventType.AUTH_SESSION_TERMINATED)["details"]["asserted_identity"]
            == OTHER_IDENTITY
        )

    def test_an_unmatched_route_still_produces_a_record(
        self, db_session, proxy_enabled, audited, proxy_user
    ):
        """No route resolved yet → the raw URL path, never a crash in the audit path."""
        with pytest.raises(HTTPException):
            _enforce(
                proxy_user,
                _request(asserted=OTHER_IDENTITY, matched_route=False),
                db_session,
            )

        assert (
            _record(audited, AuditEventType.AUTH_SESSION_TERMINATED)["details"]["path"]
            == ORDINARY_PATH
        )


# ── the gate reaches it, and reaches it first ───────────────────────────────────


class TestTheLifecycleGateAppliesTheCheck:
    """A check nothing calls is not a control."""

    def test_get_current_active_user_enforces_it(
        self, db_session, proxy_enabled, audited, proxy_user
    ):
        with pytest.raises(HTTPException) as exc:
            get_current_active_user(
                request=_request(asserted=OTHER_IDENTITY),
                current_user=proxy_user,
                db=db_session,
            )

        assert exc.value.status_code == 401
        assert exc.value.detail["code"] == ERROR_CODE_PROXY_IDENTITY_MISMATCH

    def test_a_matching_assertion_passes_the_whole_gate(
        self, db_session, proxy_enabled, audited, proxy_user
    ):
        returned = get_current_active_user(
            request=_request(asserted=str(proxy_user.email)),
            current_user=proxy_user,
            db=db_session,
        )

        assert returned is proxy_user
        assert audited == []

    def test_it_runs_before_the_other_lifecycle_gates(
        self, db_session, proxy_enabled, audited, proxy_user
    ):
        """If the session belongs to nobody, no later question is about the right
        user — so the mismatch must win over a co-occurring password-change hold,
        which would otherwise answer 403 and hide the revocation."""
        proxy_user.must_change_password = True
        proxy_user.account_expires_at = datetime.now(UTC) - timedelta(days=1)
        db_session.commit()

        with pytest.raises(HTTPException) as exc:
            get_current_active_user(
                request=_request(asserted=OTHER_IDENTITY),
                current_user=proxy_user,
                db=db_session,
            )

        assert exc.value.status_code == 401
        assert exc.value.detail["code"] == ERROR_CODE_PROXY_IDENTITY_MISMATCH

    def test_a_deactivated_account_still_gets_the_original_400(
        self, db_session, proxy_enabled, audited, proxy_user
    ):
        """The deactivation check precedes everything and must not be swallowed."""
        proxy_user.is_active = False
        db_session.commit()

        with pytest.raises(HTTPException) as exc:
            get_current_active_user(
                request=_request(asserted=OTHER_IDENTITY),
                current_user=proxy_user,
                db=db_session,
            )

        assert exc.value.status_code == 400
