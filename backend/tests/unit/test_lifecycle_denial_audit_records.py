"""What a lifecycle refusal actually *records*, and the route template it records it against.

The lifecycle gates were built and tested for the decision they make: an expired
account is refused, an unapproved one is refused, a flagged one is confined. What was
never asserted is the other half of each of those controls — the audit record
(FedRAMP AU-2). ``_audit_lifecycle_denial`` is one shared function, and everything it
is handed was unchecked: the account it names, the client it attributes the attempt
to, the error code an operator filters on, and the per-gate ``details``. A record
that says only "a denial happened, somewhere, to somebody" satisfies nothing, and
nothing raises when it degrades to that — which is exactly why it needs a test rather
than a reading.

The second half of this file is ``_route_path``, which decides which template the
exempt sets are matched against. It is the reason
``PASSWORD_CHANGE_EXEMPT_PATHS``/``BANNER_EXEMPT_PATHS`` can be a frozenset of
literals at all: it resolves the matched route (so a path parameter can never be
crafted to look exempt) and strips the trailing slash (so nginx's trailing-slash
parity does not lock a held user out of the one endpoint that can release them).

Sibling suites own the *decisions*: ``test_account_lifecycle.py`` (expiry, forced
change), ``test_account_approval.py``, ``test_banner_acknowledgment.py``. This one
owns the records and the path resolution, so neither file has to grow a second
concern.
"""

# mypy: disable-error-code="arg-type,index"
# Structural stand-ins are handed to signatures declaring ``User``/``Session``/
# ``Request``, and ``HTTPException.detail`` is typed ``str`` while every gate raises
# an object detail. Declared once here rather than as a cast per assertion.
from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from fastapi import HTTPException

from app.api.endpoints.auth import dependencies as deps_module
from app.api.endpoints.auth.dependencies import BANNER_EXEMPT_PATHS
from app.api.endpoints.auth.dependencies import ERROR_CODE_ACCOUNT_EXPIRED
from app.api.endpoints.auth.dependencies import ERROR_CODE_ACCOUNT_PENDING_APPROVAL
from app.api.endpoints.auth.dependencies import ERROR_CODE_ACCOUNT_REJECTED
from app.api.endpoints.auth.dependencies import PASSWORD_CHANGE_EXEMPT_PATHS
from app.api.endpoints.auth.dependencies import get_current_active_user
from app.auth.approval import APPROVAL_APPROVED
from app.auth.approval import APPROVAL_PENDING
from app.auth.approval import APPROVAL_REJECTED
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.core.config import settings

USER_UUID = "019ec90a-1b2c-7def-8000-0000000000cc"
ORDINARY_PATH = f"{settings.API_PREFIX}/files"
CHANGE_PASSWORD_PATH = f"{settings.API_PREFIX}/users/me"
CLIENT_IP = "198.51.100.23"
USER_AGENT = "opentranscribe-cli/1.0"


def _user(**overrides: Any) -> Any:
    attrs: dict[str, Any] = {
        "id": 4242,
        "uuid": UUID(USER_UUID),
        "email": "audited.person@example.com",
        "role": "user",
        "auth_type": "local",
        "is_active": True,
        "approval_status": APPROVAL_APPROVED,
        "must_change_password": False,
        "account_expires_at": None,
        "banner_acknowledged_at": None,
    }
    attrs.update(overrides)
    return SimpleNamespace(**attrs)


def _request(path: str = ORDINARY_PATH, *, matched_route: bool = True) -> Any:
    """A Request stand-in with a matched route, a peer address and a user agent."""
    return SimpleNamespace(
        scope={"route": SimpleNamespace(path=path)} if matched_route else {},
        url=SimpleNamespace(path=path),
        client=SimpleNamespace(host=CLIENT_IP),
        headers={"User-Agent": USER_AGENT},
        state=SimpleNamespace(),
        cookies={},
    )


@pytest.fixture
def audited(monkeypatch) -> list[dict]:
    """Capture every audit event the dependency gate emits."""
    events: list[dict] = []
    monkeypatch.setattr(deps_module.audit_logger, "log", lambda **kw: events.append(kw))
    return events


def _publish(**values: Any) -> None:
    from app.core.auth_settings import publish_process_auth_setting

    for key, value in values.items():
        publish_process_auth_setting(key, value)


def _deny(user: Any, path: str = ORDINARY_PATH) -> HTTPException:
    with pytest.raises(HTTPException) as exc:
        get_current_active_user(request=_request(path), current_user=user)
    return exc.value


# ── the shared record: whose attempt, from where ─────────────────────────────────


class TestEveryLifecycleDenialIdentifiesTheAccountAndTheClient:
    """``_audit_lifecycle_denial`` is one function; these are its four callers.

    Parameterised deliberately: the fields below are the record's entire value, and
    a gate that stopped supplying one of them would still refuse correctly and still
    pass every existing test.
    """

    GATES = {
        "expiry": {"account_expires_at": datetime.now(UTC) - timedelta(days=1)},
        "password_change": {"must_change_password": True},
        "rejected": {"approval_status": APPROVAL_REJECTED},
    }

    @pytest.mark.parametrize("gate", sorted(GATES))
    def test_the_record_names_the_account(self, audited, gate):
        user = _user(**self.GATES[gate])

        _deny(user)

        assert len(audited) == 1
        assert audited[0]["user_id"] == user.id
        assert audited[0]["username"] == user.email

    @pytest.mark.parametrize("gate", sorted(GATES))
    def test_the_record_attributes_the_client(self, audited, gate):
        """Without these, "somebody tried repeatedly" cannot be told from
        "one script is looping"."""
        _deny(_user(**self.GATES[gate]))

        assert audited[0]["source_ip"] == CLIENT_IP
        assert audited[0]["user_agent"] == USER_AGENT

    @pytest.mark.parametrize("gate", sorted(GATES))
    def test_the_record_is_a_failure(self, audited, gate):
        """The outcome is what makes it findable — an operator filters on it."""
        _deny(_user(**self.GATES[gate]))

        assert audited[0]["outcome"] is AuditOutcome.FAILURE

    @pytest.mark.parametrize("gate", sorted(GATES))
    def test_the_record_says_which_route_was_refused(self, audited, gate):
        _deny(_user(**self.GATES[gate]), path=ORDINARY_PATH)

        assert audited[0]["details"]["path"] == ORDINARY_PATH

    def test_an_allowed_request_records_nothing(self, audited):
        """Positive control: the record belongs to the refusal, not to the gate."""
        user = _user()

        assert get_current_active_user(request=_request(), current_user=user) is user
        assert audited == []

    def test_a_client_that_sends_no_user_agent_is_recorded_as_unknown(self, audited):
        """A null in that field breaks a dashboard grouping by user agent; the
        placeholder keeps the record uniformly typed."""
        anonymous_client: Any = SimpleNamespace(
            scope={"route": SimpleNamespace(path=ORDINARY_PATH)},
            url=SimpleNamespace(path=ORDINARY_PATH),
            client=SimpleNamespace(host=CLIENT_IP),
            headers={},
            state=SimpleNamespace(),
        )

        with pytest.raises(HTTPException):
            get_current_active_user(
                request=anonymous_client, current_user=_user(must_change_password=True)
            )

        assert audited[0]["user_agent"] == "unknown"
        assert audited[0]["source_ip"] == CLIENT_IP

    def test_an_odd_request_object_never_costs_the_decision(self, audited):
        """An audit record must not be the reason an authorization decision fails,
        so client attribution degrades to "unknown" rather than raising."""
        broken: Any = SimpleNamespace(
            scope={}, url=SimpleNamespace(path=ORDINARY_PATH), state=SimpleNamespace()
        )

        with pytest.raises(HTTPException) as exc:
            get_current_active_user(request=broken, current_user=_user(must_change_password=True))

        assert exc.value.status_code == 403
        assert audited[0]["source_ip"] == "unknown"
        assert audited[0]["user_agent"] == "unknown"


# ── per-gate: the event type, the error code, the details ────────────────────────


class TestTheExpiryRecord:
    def test_it_is_an_account_expired_event(self, audited):
        expires_at = datetime.now(UTC) - timedelta(days=3)

        assert _deny(_user(account_expires_at=expires_at)).status_code == 403
        assert audited[0]["event_type"] is AuditEventType.AUTH_ACCOUNT_EXPIRED
        assert audited[0]["error_code"] == "ACCOUNT_EXPIRED"

    def test_it_records_when_the_account_expired(self, audited):
        """The remedy is "extend it", so the operator needs the date, not just the fact."""
        expires_at = datetime.now(UTC) - timedelta(days=3)

        _deny(_user(account_expires_at=expires_at))

        assert audited[0]["details"]["expired_at"] == expires_at.isoformat()

    def test_the_refusal_names_the_code_the_spa_branches_on(self, audited):
        exc = _deny(_user(account_expires_at=datetime.now(UTC) - timedelta(days=3)))

        assert exc.detail["code"] == ERROR_CODE_ACCOUNT_EXPIRED


class TestThePasswordChangeRecord:
    def test_it_is_a_password_expired_event(self, audited):
        assert _deny(_user(must_change_password=True)).status_code == 403
        assert audited[0]["event_type"] is AuditEventType.AUTH_PASSWORD_EXPIRED
        assert audited[0]["error_code"] == "PASSWORD_CHANGE_REQUIRED"


class TestTheApprovalRecords:
    """Rejected and pending are different states and must not share a code.

    Telling a refused applicant they are "pending" is simply false, and an operator
    working an approval queue needs to see which of the two happened.
    """

    def test_a_rejected_account_is_recorded_as_rejected(self, audited):
        exc = _deny(_user(approval_status=APPROVAL_REJECTED))

        assert exc.status_code == 403
        assert exc.detail["code"] == ERROR_CODE_ACCOUNT_REJECTED
        assert audited[0]["error_code"] == "ACCOUNT_REJECTED"

    def test_a_pending_account_is_recorded_as_pending(self, audited):
        _publish(require_account_approval=True)

        exc = _deny(_user(approval_status=APPROVAL_PENDING))

        assert exc.status_code == 403
        assert exc.detail["code"] == ERROR_CODE_ACCOUNT_PENDING_APPROVAL
        assert audited[0]["error_code"] == "ACCOUNT_PENDING_APPROVAL"

    def test_the_error_code_is_upper_case_like_every_other_gate(self, audited):
        """It is derived from the ``detail.code`` rather than written twice, so the
        casing is a real risk: an audit consumer matching ``ACCOUNT_REJECTED``
        silently stops matching if it arrives lower-cased."""
        _deny(_user(approval_status=APPROVAL_REJECTED))

        assert audited[0]["error_code"] == audited[0]["error_code"].upper()
        assert audited[0]["error_code"] != ERROR_CODE_ACCOUNT_REJECTED

    def test_the_record_carries_the_status_that_caused_it(self, audited):
        _deny(_user(approval_status=APPROVAL_REJECTED))

        assert audited[0]["details"]["approval_status"] == APPROVAL_REJECTED

    def test_both_approval_states_are_the_same_event_type(self, audited):
        _publish(require_account_approval=True)

        _deny(_user(approval_status=APPROVAL_PENDING))

        assert audited[0]["event_type"] is AuditEventType.AUTH_ACCOUNT_DISABLED

    def test_turning_the_setting_off_releases_pending_but_not_rejected(self, audited):
        """The operator's escape hatch, and the one state it does not open."""
        _publish(require_account_approval=False)
        pending = _user(approval_status=APPROVAL_PENDING)

        assert get_current_active_user(request=_request(), current_user=pending) is pending
        assert audited == []

        assert _deny(_user(approval_status=APPROVAL_REJECTED)).status_code == 403


class TestTheBannerRefusalIsDeliberatelyNotAudited:
    def test_no_audit_event_is_emitted(self, audited, monkeypatch):
        """Unlike the gates above it fires on EVERY request of every
        pre-acknowledgment session, so per-request events would swamp the audit
        index. The acknowledgment itself is the AC-8 artefact."""
        monkeypatch.setattr(settings, "LOGIN_BANNER_ENABLED", True)

        exc = _deny(_user(banner_acknowledged_at=None))

        assert exc.status_code == 403
        assert exc.detail["reason"] == "never_acknowledged"
        assert audited == []


# ── _route_path: what the exempt sets are matched against ────────────────────────


class TestTheExemptSetsMatchTheRouteTemplate:
    """The gate's own exemptions live inside it as a path check, so the resolution
    of that path is load-bearing for whether a held user has any way out."""

    def test_the_remedy_route_is_reachable_while_held(self, audited):
        """``PUT /users/me`` is what CLEARS the flag; behind the flag there is no exit."""
        user = _user(must_change_password=True)

        assert (
            get_current_active_user(request=_request(CHANGE_PASSWORD_PATH), current_user=user)
            is user
        )

    def test_a_trailing_slash_does_not_defeat_the_exemption(self, audited):
        """nginx normalises with a trailing slash; matching literally against the
        exempt frozenset would 403 the one endpoint that can release the user."""
        user = _user(must_change_password=True)
        request = _request(f"{CHANGE_PASSWORD_PATH}/")

        assert get_current_active_user(request=request, current_user=user) is user

    def test_a_path_parameter_cannot_be_crafted_to_look_exempt(self, audited):
        """The matched TEMPLATE is used, not the raw URL, so a caller cannot name a
        resource ``users/me`` and be let through on somebody else's route."""
        user = _user(must_change_password=True)
        crafted: Any = SimpleNamespace(
            scope={"route": SimpleNamespace(path=f"{settings.API_PREFIX}/files/{{file_uuid}}")},
            url=SimpleNamespace(path=CHANGE_PASSWORD_PATH),
            client=SimpleNamespace(host=CLIENT_IP),
            headers={"User-Agent": USER_AGENT},
            state=SimpleNamespace(),
        )

        with pytest.raises(HTTPException) as exc:
            get_current_active_user(request=crafted, current_user=user)

        assert exc.value.status_code == 403

    def test_an_unresolved_route_fails_closed(self, audited):
        """No route matched yet → the raw URL, which is not in the exempt set."""
        user = _user(must_change_password=True)

        with pytest.raises(HTTPException) as exc:
            get_current_active_user(
                request=_request(ORDINARY_PATH, matched_route=False), current_user=user
            )

        assert exc.value.status_code == 403
        # And the URL fallback is what keeps the record useful: without it an
        # unmatched request is refused and audited against no path at all.
        assert audited[0]["details"]["path"] == ORDINARY_PATH

    def test_the_root_path_is_left_alone(self, audited):
        """``"/"`` is one character; stripping it would leave the empty string, and
        the audit record would name no route at all."""
        assert deps_module._route_path(_request("/")) == "/"

    def test_a_request_stand_in_with_no_path_resolves_to_the_empty_string(self):
        """Fails safe: an unknown path is in no exempt set, so the caller is refused."""
        assert deps_module._route_path(SimpleNamespace()) == ""

    def test_none_resolves_to_the_empty_string(self):
        assert deps_module._route_path(None) == ""

    def test_both_exempt_sets_are_stored_without_trailing_slashes(self):
        """The stripping above is only correct if the sets are normalised the same
        way — a stored ``/api/users/me/`` could never be matched."""
        for path in PASSWORD_CHANGE_EXEMPT_PATHS | BANNER_EXEMPT_PATHS:
            assert not path.endswith("/"), path
        assert len(PASSWORD_CHANGE_EXEMPT_PATHS) == 3
        assert len(BANNER_EXEMPT_PATHS) == 4
