"""Account-enforcement behaviour that no test asserted (issue #446, mutation survivors).

The lifecycle-gate half of ``test_dependencies_survivor_mutants.py``, split with
``app/api/endpoints/auth/dependencies.py`` into ``lifecycle_gates.py`` and
``external_token.py``. The full triage of the module's survivors, and the per-file
attribution of the measured survivor count after the split, is in that file's docstring.
"""

# mypy: disable-error-code="arg-type,comparison-overlap,index"
# This suite passes structural stand-ins (fake sessions, fake users, namespace
# requests) to signatures declaring Session/User/Request, and indexes
# HTTPException.detail, which is typed str while every lifecycle gate raises an
# object -- the same shape test_lockout_survivor_mutants.py and
# test_banner_acknowledgment.py suppress for the same reason. Declared once here
# rather than as a cast at every call site.
from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.endpoints.auth import dependencies as deps_module
from app.api.endpoints.auth import external_token as external_token_module
from app.api.endpoints.auth import lifecycle_gates as gates_module
from app.api.endpoints.auth.dependencies import get_current_active_user
from app.api.endpoints.auth.external_token import ERROR_CODE_PROXY_IDENTITY_MISMATCH
from app.api.endpoints.auth.lifecycle_gates import ERROR_CODE_ACCOUNT_EXPIRED
from app.api.endpoints.auth.lifecycle_gates import ERROR_CODE_ACCOUNT_PENDING_APPROVAL
from app.api.endpoints.auth.lifecycle_gates import ERROR_CODE_ACCOUNT_REJECTED
from app.api.endpoints.auth.lifecycle_gates import ERROR_CODE_BANNER_ACKNOWLEDGMENT_REQUIRED
from app.api.endpoints.auth.lifecycle_gates import ERROR_CODE_PASSWORD_CHANGE_REQUIRED
from app.core.config import settings
from tests.unit.auth_dependencies._helpers import ORDINARY_PATH
from tests.unit.auth_dependencies._helpers import _real_request
from tests.unit.auth_dependencies._helpers import _request
from tests.unit.auth_dependencies._helpers import _user

# ── _audit_lifecycle_denial: the shared audit-record builder ─────────────────────


class TestAuditLifecycleDenialUsernameFallback:
    def test_a_falsy_email_is_recorded_as_an_empty_string_not_a_sentinel(self, monkeypatch):
        """``str(getattr(user, "email", "") or "")`` — the trailing fallback must
        stay ``""``, not a sentinel like ``"XXXX"``. Unreachable for any real,
        persisted ``User`` (``email`` is a ``NOT NULL`` column set at registration),
        but the audit record must still degrade safely rather than fabricate an
        identity string for the rare/defensive case where it is falsy.
        """
        recorded: list[dict] = []
        monkeypatch.setattr(gates_module.audit_logger, "log", lambda **kw: recorded.append(kw))
        user = _user(email="")

        gates_module._audit_lifecycle_denial(
            gates_module.AuditEventType.AUTH_ACCOUNT_EXPIRED,
            user,
            _request(),
            error_code="ACCOUNT_EXPIRED",
            details={},
        )

        assert len(recorded) == 1
        assert recorded[0]["username"] == ""


# ── _route_path: the fail-safe input to every exempt-path check ──────────────────


class TestRoutePathStripsOnlyATrailingSlash:
    def test_a_path_ending_in_x_is_not_stripped(self):
        """``rstrip("/")``, not a wider character class.

        A mutant widening the strip set to ``"XX/XX"`` would also strip trailing
        ``X`` characters — invisible on any real route template, but this function's
        whole job is matching untrusted/edge-case paths against the exempt sets, and
        a widened strip set is a real, if narrow, matching bug.
        """
        request = _request("/api/userX")

        assert gates_module._route_path(request) == "/api/userX"

    def test_a_two_character_path_of_only_slashes_is_fully_stripped(self):
        """``len(path) > 1``, not ``> 2``: the boundary a two-character path sits on.

        ``"//"`` has length 2. Under the real guard that is already ``> 1``, so it
        is stripped to ``""``. A mutant guard of ``> 2`` would leave it as ``"//"``
        instead — the smallest input where the two guards disagree.
        """
        request = _request("//")

        assert gates_module._route_path(request) == ""


# ── _banner_requirement: the fallback that keeps a broken session non-fatal ──────


class TestBannerRequirementFallsBackOnAnUnusableSession:
    def test_a_non_none_session_without_query_still_falls_back(self, monkeypatch):
        """A caller can hand in something that is not ``None`` but still cannot
        query (a broken/half-initialised session stand-in); the function must still
        degrade to the ``.env`` value rather than raising.

        This does NOT kill ``x__banner_requirement__mutmut_1`` (``or`` -> ``and``
        in the guard) — confirmed EQUIVALENT by ``--verify`` after this test was
        written and still reported SURVIVED. Proof: ``hasattr(None, "query")`` is
        already ``False``, so ``db is None`` is a strict subset of
        ``not hasattr(db, "query")`` — the first disjunct never adds a case the
        second does not already cover. Three cases, all identical either way:
        ``db is None`` -> both forms are ``True`` -> fallback. ``db`` present but
        no ``.query`` -> the ``or`` form is ``True`` (immediate fallback); the
        ``and`` form is ``False``, but the enclosing ``try/except Exception`` then
        catches the ``AttributeError`` from calling the missing ``.query`` and
        returns the SAME fallback. ``db`` present with ``.query`` -> both forms are
        ``False`` -> both proceed to the real read. No object can separate the two
        forms, so this is a floor, not a gap; kept as a correctness test, not a
        mutant-killing one.
        """
        monkeypatch.setattr(settings, "LOGIN_BANNER_ENABLED", True)
        no_query = SimpleNamespace()  # deliberately no .query attribute

        enabled, changed_at = gates_module._banner_requirement(no_query)

        assert enabled is True
        assert changed_at is None

    def test_a_failing_config_query_falls_back_to_the_env_value_not_to_off(self, monkeypatch):
        """The ``except Exception`` branch returns ``settings.LOGIN_BANNER_ENABLED``.

        A real session whose query raises (a dropped connection) must leave the gate
        as ``.env`` configures it. Returning ``False`` there would switch the AC-8
        banner off for every request during a database hiccup.
        """

        class _QueryRaises:
            def query(self, model):
                raise RuntimeError("connection reset")

        monkeypatch.setattr(settings, "LOGIN_BANNER_ENABLED", True)

        enabled, changed_at = gates_module._banner_requirement(_QueryRaises())

        assert enabled is True
        assert changed_at is None


# ── _enforce_account_expiry: the boundary and the response body ──────────────────


class TestEnforceAccountExpiry:
    def test_the_exact_expiry_instant_is_already_treated_as_expired(self):
        """``datetime.now(UTC) < expires_at`` — not ``<=``.

        The early-return guard only lets the caller through while ``now`` is
        STRICTLY before ``expires_at``; at the exact instant they are equal, ``<``
        is ``False`` and the account is refused. The ``<=`` mutant would instead
        return (allow access) at that exact instant — one instant later than the
        real code permits.
        """
        frozen = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

        class _Frozen(datetime):
            @classmethod
            def now(cls, tz=None):  # noqa: ARG003
                return frozen

        user = _user(account_expires_at=frozen)
        import unittest.mock as mock

        with mock.patch.object(gates_module, "datetime", _Frozen):
            with pytest.raises(HTTPException) as exc:
                gates_module._enforce_account_expiry(user, _request())

        assert exc.value.status_code == 403

    def test_the_response_body_is_exactly_this_shape(self):
        expires_at = datetime(2024, 6, 15, tzinfo=UTC)
        user = _user(account_expires_at=expires_at)

        with pytest.raises(HTTPException) as exc:
            gates_module._enforce_account_expiry(user, _request())

        assert exc.value.status_code == 403
        assert exc.value.detail == {
            "code": ERROR_CODE_ACCOUNT_EXPIRED,
            "message": (
                "This account expired on 2024-06-15. Contact an administrator to extend it."
            ),
        }

    def test_the_audit_details_are_tagged_with_the_fixed_date_trigger(self, monkeypatch):
        """``details={"trigger": "fixed_date", ...}`` on the ``AUTH_ACCOUNT_EXPIRED`` audit call.

        ``account_lifecycle_service.py``'s background inactivity sweep emits the SAME
        event type with ``details.trigger == "inactivity"`` (or
        ``"inactivity_skipped_super_admin"``). Per that module's own docstring, a
        FedRAMP AU-2 reader "must not have to string-match ``details.trigger`` to tell
        the two apart" — so a renamed key or reworded value here is not cosmetic, it
        breaks the one field that disambiguates a per-request refusal from a
        background sweep in the audit trail. Without this assertion, mutating
        ``"trigger"`` -> ``"XXtriggerXX"`` or ``"fixed_date"`` -> ``"FIXED_DATE"``
        survived (issue #446 follow-up: introduced by the trigger key landing in
        1b536070 without a test alongside it).
        """
        recorded: list[dict] = []
        monkeypatch.setattr(gates_module.audit_logger, "log", lambda **kw: recorded.append(kw))
        expires_at = datetime(2024, 6, 15, tzinfo=UTC)
        user = _user(account_expires_at=expires_at)

        with pytest.raises(HTTPException):
            gates_module._enforce_account_expiry(user, _request())

        assert len(recorded) == 1
        assert recorded[0]["details"] == {
            "trigger": "fixed_date",
            "expired_at": expires_at.isoformat(),
            "path": ORDINARY_PATH,
        }


# ── _enforce_approval: both refusal shapes, and the db argument ──────────────────


class TestEnforceApprovalResponseBodies:
    def test_a_rejected_account_gets_exactly_this_body(self, db_session):
        user = _user(approval_status="rejected")

        with pytest.raises(HTTPException) as exc:
            gates_module._enforce_approval(user, _request(), db_session)

        assert exc.value.status_code == 403
        assert exc.value.detail == {
            "code": ERROR_CODE_ACCOUNT_REJECTED,
            "message": "This account was not approved. Contact an administrator.",
        }

    def test_a_pending_account_gets_exactly_this_body(self, db_session, monkeypatch):
        from app.auth import approval as approval_module

        monkeypatch.setattr(approval_module, "approval_required", lambda db: True)
        user = _user(approval_status="pending")

        with pytest.raises(HTTPException) as exc:
            gates_module._enforce_approval(user, _request(), db_session)

        assert exc.value.status_code == 403
        assert exc.value.detail == {
            "code": ERROR_CODE_ACCOUNT_PENDING_APPROVAL,
            "message": "This account is awaiting administrator approval.",
        }


class TestEnforceApprovalPassesTheRealSession:
    def test_approval_required_receives_the_callers_db_not_none(self, db_session, monkeypatch):
        """A dropped ``db`` argument would make ``approval_required`` fall back to
        the process-level cache instead of a fresh per-request DB read — the two
        can disagree in the window right after an admin flips the setting.
        """
        from app.auth import approval as approval_module

        received: list[object] = []

        def _spy(db):
            received.append(db)
            return True

        monkeypatch.setattr(approval_module, "approval_required", _spy)
        user = _user(approval_status="pending")

        with pytest.raises(HTTPException):
            gates_module._enforce_approval(user, _request(), db_session)

        assert received == [db_session]

    def test_get_current_active_user_passes_its_own_db_to_enforce_approval(
        self, db_session, monkeypatch
    ):
        received: list[object] = []

        def _spy(user, request, db):
            received.append(db)

        monkeypatch.setattr(deps_module, "_enforce_approval", _spy)
        user = _user()

        get_current_active_user(request=_request(), current_user=user, db=db_session)

        assert received == [db_session]


# ── _enforce_banner_acknowledgment: normalization, boundary, response body ───────


class _ConfigRow(SimpleNamespace):
    pass


class _ConfigQuery:
    def __init__(self, rows: list):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return self._rows


class _FakeBannerDB:
    def __init__(self, rows: list):
        self.rows = rows

    def query(self, model):
        return _ConfigQuery(self.rows)


def _banner_rows(text_updated_at: datetime) -> list:
    return [
        _ConfigRow(
            config_key="login_banner_enabled", config_value="true", updated_at=datetime.now(UTC)
        ),
        _ConfigRow(
            config_key="login_banner_text", config_value="ATTENTION", updated_at=text_updated_at
        ),
    ]


class TestBannerAcknowledgmentNaiveTextTimestamp:
    def test_a_naive_text_change_timestamp_does_not_crash_the_compare(self):
        """``changed_at.tzinfo is None`` -> normalise to UTC — not the inverse guard,
        and not dropped to ``None``/left naive.

        The stored ``login_banner_text`` row's ``updated_at`` can come back naive
        (a write that did not set tzinfo). The comparison two lines later is against
        an already-aware ``acknowledged_at``; a naive ``changed_at`` reaching it
        raises ``TypeError`` — a 500 on every authenticated request, not a 403.
        """
        naive_changed_at = datetime(2026, 1, 1, 12, 0)  # deliberately no tzinfo
        user = _user(banner_acknowledged_at=datetime(2025, 1, 1, tzinfo=UTC))
        db = _FakeBannerDB(_banner_rows(naive_changed_at))

        with pytest.raises(HTTPException) as exc:
            gates_module._enforce_banner_acknowledgment(
                user, _request(), db
            )  # must not raise TypeError

        assert exc.value.detail["reason"] == "banner_text_changed"

    def test_an_acknowledgment_after_a_naive_text_change_still_passes(self):
        """The other half: normalise correctly enough that a FRESH ack clears it."""
        naive_changed_at = datetime(2026, 1, 1, 12, 0)
        user = _user(banner_acknowledged_at=datetime(2026, 1, 2, tzinfo=UTC))
        db = _FakeBannerDB(_banner_rows(naive_changed_at))

        refused = False
        try:
            gates_module._enforce_banner_acknowledgment(user, _request(), db)
        except HTTPException:
            refused = True
        assert refused is False, "a fresh acknowledgment must not be refused"


class TestBannerAcknowledgmentExpiryBoundary:
    def test_an_acknowledgment_at_the_exact_text_change_instant_still_passes(self):
        """``acknowledged_at >= changed_at`` — not ``>``.

        Acknowledging at the exact instant the text changed must count: the ``>``
        mutant would treat that instant as stale and demand a second click.
        """
        instant = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        user = _user(banner_acknowledged_at=instant)
        db = _FakeBannerDB(_banner_rows(instant))

        refused = False
        try:
            gates_module._enforce_banner_acknowledgment(user, _request(), db)
        except HTTPException:
            refused = True
        assert refused is False, "the exact expiry instant must not be treated as stale"


class TestBannerAcknowledgmentResponseBody:
    def test_the_response_body_is_exactly_this_shape(self):
        user = _user(banner_acknowledged_at=None)
        db = _FakeBannerDB(_banner_rows(datetime.now(UTC) - timedelta(days=1)))

        with pytest.raises(HTTPException) as exc:
            gates_module._enforce_banner_acknowledgment(user, _request(), db)

        assert exc.value.status_code == 403
        assert exc.value.detail == {
            "code": ERROR_CODE_BANNER_ACKNOWLEDGMENT_REQUIRED,
            "message": "You must acknowledge the login banner before continuing.",
            "reason": "never_acknowledged",
        }


# ── _enforce_password_change: the response body ───────────────────────────────────


class TestEnforcePasswordChangeResponseBody:
    def test_the_response_body_is_exactly_this_shape(self):
        user = _user(must_change_password=True)

        with pytest.raises(HTTPException) as exc:
            gates_module._enforce_password_change(user, _request())

        assert exc.value.status_code == 403
        assert exc.value.detail == {
            "code": ERROR_CODE_PASSWORD_CHANGE_REQUIRED,
            "message": "You must change your password before continuing.",
        }


# ── _enforce_proxy_identity_consistency: the response body ───────────────────────


class TestEnforceProxyIdentityMismatchResponseBody:
    def test_the_response_body_is_exactly_this_shape(self, db_session, monkeypatch):
        from app.core.auth_types import AUTH_TYPE_PROXY
        from app.core.process_auth_settings import publish_process_auth_setting

        publish_process_auth_setting("proxy_enabled", True)
        publish_process_auth_setting("proxy_email_header", "X-Remote-User")
        publish_process_auth_setting("proxy_trusted_proxies", "10.0.0.0/8")
        monkeypatch.setattr(
            "app.services.account_security_service.revoke_all_sessions", lambda *a, **kw: 1
        )
        monkeypatch.setattr(db_session, "commit", lambda: None)
        user = _user(auth_type=AUTH_TYPE_PROXY, email="real@example.com")
        request = _real_request(headers=[(b"x-remote-user", b"someone-else@example.com")])
        request.scope["client"] = ("10.0.0.9", 40000)

        with pytest.raises(HTTPException) as exc:
            external_token_module._enforce_proxy_identity_consistency(user, request, db_session)

        assert exc.value.status_code == 401
        assert exc.value.detail == {
            "code": ERROR_CODE_PROXY_IDENTITY_MISMATCH,
            "message": "Identity mismatch. Please sign in again.",
        }


# ── exempt paths: exact route templates, never prefixes ───────────────────────────


class TestExemptPathsMatchExactlyNotByPrefix:
    """``path in <EXEMPT_PATHS>`` is an exact match on the route template.

    A prefix match would exempt every route nested under an exempt one, so a caller
    who owes a password change (or a banner acknowledgment) could use any
    ``/users/me/...`` (or ``/auth/banner/...``) route while the gate was meant to
    confine them to the one endpoint that clears it.
    """

    def test_a_route_nested_under_users_me_is_still_refused_for_a_password_change(self):
        user = _user(must_change_password=True)

        with pytest.raises(HTTPException) as exc:
            gates_module._enforce_password_change(
                user, _request(f"{settings.API_PREFIX}/users/me/settings")
            )

        assert exc.value.detail["code"] == ERROR_CODE_PASSWORD_CHANGE_REQUIRED

    def test_a_route_nested_under_the_banner_endpoint_is_still_refused(self):
        user = _user(banner_acknowledged_at=None)
        db = _FakeBannerDB(_banner_rows(datetime.now(UTC) - timedelta(days=1)))

        with pytest.raises(HTTPException) as exc:
            gates_module._enforce_banner_acknowledgment(
                user, _request(f"{settings.API_PREFIX}/auth/banner/acknowledge/history"), db
            )

        assert exc.value.detail["code"] == ERROR_CODE_BANNER_ACKNOWLEDGMENT_REQUIRED


# ── _get_client_info: the audited address goes through the trusted-proxy chain ───


class TestGetClientInfoResolvesThroughTrustedProxies:
    def test_a_trusted_proxy_hop_is_stepped_over_not_recorded(self, monkeypatch):
        """``resolve_client_ip(request)`` — not ``request.client.host``.

        Behind a reverse proxy the raw peer is the proxy, so recording it would
        attribute every audited login to the proxy's address (issue #284 A0.5).
        """
        import ipaddress

        import app.utils.client_ip as client_ip

        monkeypatch.setattr(client_ip, "_TRUSTED_NETWORKS", [ipaddress.ip_network("10.0.0.0/8")])
        request = _real_request(
            headers=[(b"user-agent", b"pytest-agent"), (b"x-forwarded-for", b"203.0.113.9")]
        )

        assert gates_module._get_client_info(request) == ("203.0.113.9", "pytest-agent")
