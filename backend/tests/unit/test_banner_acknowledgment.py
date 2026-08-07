"""Login-banner acknowledgment is a precondition for access (FedRAMP AC-8).

``user.banner_acknowledged_at`` was written by ``POST /auth/banner/acknowledge``
— with correct auditing, and a docstring stating it "must be called after login
before granting full access" — and read by **nothing**. The SPA approximated the
control with a ``sessionStorage`` flag: cleared per tab, removable from the
console, and never sent to the server, so the consent AC-8 requires was never a
precondition for anything.

The gate now lives in ``get_current_active_user``, beside the
``must_change_password`` / ``account_expires_at`` gates and following the same
shape: a machine-readable ``detail.code``, exempt routes matched against the
resolved route template, and the endpoint that CLEARS the condition kept
reachable.

It also expires an acknowledgment when the banner **text** changes: a user who
accepted different wording has not accepted this one. That comparison is against
the ``login_banner_text`` config row's ``updated_at``, so it needs no column of
its own.

Everything here runs against fakes: no Postgres, no Redis, no HTTP client.
"""

# mypy: disable-error-code="arg-type,index,method-assign"
# These tests pass structural stand-ins to signatures declaring Session/User
# and index HTTPException.detail, which is declared str while every lifecycle
# gate raises an object. Suppressing both for the file is the honest statement.
from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException
from fastapi import Response

from app.api.endpoints.auth.dependencies import BANNER_EXEMPT_PATHS
from app.api.endpoints.auth.dependencies import ERROR_CODE_BANNER_ACKNOWLEDGMENT_REQUIRED
from app.api.endpoints.auth.dependencies import ERROR_CODE_PASSWORD_CHANGE_REQUIRED
from app.api.endpoints.auth.dependencies import get_current_active_user
from app.core.config import settings

USER_UUID = "019ec90a-1b2c-7def-8000-0000000000dd"

ACKNOWLEDGE_PATH = f"{settings.API_PREFIX}/auth/banner/acknowledge"
BANNER_PATH = f"{settings.API_PREFIX}/auth/banner"
LOGOUT_PATH = f"{settings.API_PREFIX}/auth/logout"
LOGOUT_ALL_PATH = f"{settings.API_PREFIX}/auth/logout/all"
ORDINARY_PATH = f"{settings.API_PREFIX}/files"


# ── fakes ────────────────────────────────────────────────────────────────────────


class _ConfigRow(SimpleNamespace):
    """An ``auth_config`` row: value plus the ``updated_at`` the gate compares against."""


class _ConfigQuery:
    def __init__(self, rows: list):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    """Minimal ``Session`` stand-in serving the two banner config rows."""

    def __init__(self, rows: list | None = None):
        self.rows = rows or []
        self.queries = 0

    def query(self, model):
        self.queries += 1
        return _ConfigQuery(self.rows)

    def commit(self):
        pass


def _banner_rows(enabled: bool = True, text_updated_at: datetime | None = None) -> list:
    """The stored configuration a banner-enabled deployment has."""
    return [
        _ConfigRow(
            config_key="login_banner_enabled",
            config_value="true" if enabled else "false",
            updated_at=datetime.now(UTC),
        ),
        _ConfigRow(
            config_key="login_banner_text",
            config_value="AUTHORIZED USE ONLY",
            updated_at=text_updated_at or datetime.now(UTC) - timedelta(days=30),
        ),
    ]


def _user(**overrides):
    """A ``User`` stand-in with the attributes the lifecycle gates read."""
    attrs = {
        "id": 7,
        "uuid": UUID(USER_UUID),
        "email": "person@example.com",
        "role": "user",
        "auth_type": "local",
        "is_active": True,
        "must_change_password": False,
        "account_expires_at": None,
        "banner_acknowledged_at": None,
    }
    attrs.update(overrides)
    return SimpleNamespace(**attrs)


def _request(path: str = ORDINARY_PATH) -> SimpleNamespace:
    """A Request stand-in carrying a matched route, as Starlette would."""
    return SimpleNamespace(
        scope={"route": SimpleNamespace(path=path)},
        url=SimpleNamespace(path=path),
        client=SimpleNamespace(host="10.0.0.1"),
        headers={"User-Agent": "pytest"},
        state=SimpleNamespace(),
        cookies={},
    )


@pytest.fixture
def banner_off(monkeypatch):
    """No stored configuration and the .env default — the gate must not engage."""
    monkeypatch.setattr(settings, "LOGIN_BANNER_ENABLED", False)


# ── the gate refuses an unacknowledged caller ────────────────────────────────────


class TestUnacknowledgedAccessIsRefused:
    def test_unacknowledged_user_is_refused(self, banner_off):
        with pytest.raises(HTTPException) as exc:
            get_current_active_user(
                request=_request(ORDINARY_PATH),
                current_user=_user(),
                db=_FakeDB(_banner_rows()),
            )

        assert exc.value.status_code == 403

    def test_refusal_carries_a_machine_readable_code(self, banner_off):
        """The SPA branches on detail.code; English prose is not a contract."""
        with pytest.raises(HTTPException) as exc:
            get_current_active_user(
                request=_request(ORDINARY_PATH),
                current_user=_user(),
                db=_FakeDB(_banner_rows()),
            )

        assert exc.value.detail["code"] == ERROR_CODE_BANNER_ACKNOWLEDGMENT_REQUIRED
        assert exc.value.detail["reason"] == "never_acknowledged"
        assert exc.value.detail["message"]

    def test_an_unmatched_path_fails_closed(self, banner_off):
        """No route resolved → not exempt. The gate must not open on uncertainty."""
        blank = SimpleNamespace(scope={}, client=None, headers={}, state=SimpleNamespace())

        with pytest.raises(HTTPException) as exc:
            get_current_active_user(request=blank, current_user=_user(), db=_FakeDB(_banner_rows()))

        assert exc.value.detail["code"] == ERROR_CODE_BANNER_ACKNOWLEDGMENT_REQUIRED

    def test_the_gate_precedes_the_password_change_gate(self, banner_off):
        """AC-8 wants consent before access, so the banner is answered first."""
        user = _user(must_change_password=True)

        with pytest.raises(HTTPException) as exc:
            get_current_active_user(
                request=_request(ORDINARY_PATH), current_user=user, db=_FakeDB(_banner_rows())
            )

        assert exc.value.detail["code"] == ERROR_CODE_BANNER_ACKNOWLEDGMENT_REQUIRED

    def test_clearing_the_banner_reveals_the_password_gate(self, banner_off):
        """…and once acknowledged, the next condition applies normally."""
        user = _user(must_change_password=True, banner_acknowledged_at=datetime.now(UTC))

        with pytest.raises(HTTPException) as exc:
            get_current_active_user(
                request=_request(ORDINARY_PATH), current_user=user, db=_FakeDB(_banner_rows())
            )

        assert exc.value.detail["code"] == ERROR_CODE_PASSWORD_CHANGE_REQUIRED


# ── the routes that let a user clear it stay reachable ───────────────────────────


class TestEscapeHatchesStayReachable:
    @pytest.mark.parametrize("path", [ACKNOWLEDGE_PATH, BANNER_PATH, LOGOUT_PATH, LOGOUT_ALL_PATH])
    def test_exempt_route_is_reachable(self, banner_off, path):
        """Without the acknowledge route the user could never clear the gate."""
        user = _user()

        assert (
            get_current_active_user(
                request=_request(path), current_user=user, db=_FakeDB(_banner_rows())
            )
            is user
        )

    def test_the_exempt_set_is_exactly_these_routes(self):
        """Pinned so a broad prefix cannot be introduced without a test failing."""
        assert (
            frozenset({ACKNOWLEDGE_PATH, BANNER_PATH, LOGOUT_PATH, LOGOUT_ALL_PATH})
            == BANNER_EXEMPT_PATHS
        )

    def test_a_lookalike_path_is_not_exempt(self, banner_off):
        """Exemption is on the resolved route template, not a prefix match."""
        with pytest.raises(HTTPException):
            get_current_active_user(
                request=_request(f"{ACKNOWLEDGE_PATH}/../files"),
                current_user=_user(),
                db=_FakeDB(_banner_rows()),
            )


# ── acknowledging clears it ──────────────────────────────────────────────────────


class TestAcknowledgingClearsTheGate:
    def test_an_acknowledged_user_passes(self, banner_off):
        user = _user(banner_acknowledged_at=datetime.now(UTC))

        assert (
            get_current_active_user(
                request=_request(ORDINARY_PATH), current_user=user, db=_FakeDB(_banner_rows())
            )
            is user
        )

    def test_the_endpoint_stamps_the_column(self, banner_off):
        """The write side, exercised directly — no HTTP client, no rate limiter."""
        from app.api.endpoints.auth import methods as methods_module

        user = _user()
        recorded: list[dict] = []
        original = methods_module.audit_logger.log
        methods_module.audit_logger.log = lambda **kw: recorded.append(kw)
        try:
            handler = methods_module.acknowledge_banner
            while hasattr(handler, "__wrapped__"):
                handler = handler.__wrapped__
            result = handler(
                request=_request(ACKNOWLEDGE_PATH),
                response=Response(),
                db=_FakeDB(),
                current_user=user,
            )
        finally:
            methods_module.audit_logger.log = original

        assert result == {"acknowledged": True}
        assert user.banner_acknowledged_at is not None
        assert [e["event_type"].value for e in recorded] == ["auth.banner.acknowledged"]

    def test_a_naive_timestamp_is_treated_as_utc(self, banner_off):
        """The column is timezone-aware, but a naive value must not crash the compare."""
        user = _user(banner_acknowledged_at=datetime.now(UTC).replace(tzinfo=None))

        assert (
            get_current_active_user(
                request=_request(ORDINARY_PATH), current_user=user, db=_FakeDB(_banner_rows())
            )
            is user
        )


# ── the acknowledgment expires when the wording changes ──────────────────────────


class TestAcknowledgmentExpiresWithTheText:
    def test_an_acknowledgment_older_than_the_text_is_refused(self, banner_off):
        """Accepting "UNCLASSIFIED" is not accepting whatever replaced it."""
        user = _user(banner_acknowledged_at=datetime.now(UTC) - timedelta(days=2))
        rows = _banner_rows(text_updated_at=datetime.now(UTC) - timedelta(hours=1))

        with pytest.raises(HTTPException) as exc:
            get_current_active_user(
                request=_request(ORDINARY_PATH), current_user=user, db=_FakeDB(rows)
            )

        assert exc.value.detail["code"] == ERROR_CODE_BANNER_ACKNOWLEDGMENT_REQUIRED
        assert exc.value.detail["reason"] == "banner_text_changed"

    def test_an_acknowledgment_after_the_edit_still_passes(self, banner_off):
        user = _user(banner_acknowledged_at=datetime.now(UTC))
        rows = _banner_rows(text_updated_at=datetime.now(UTC) - timedelta(hours=1))

        assert (
            get_current_active_user(
                request=_request(ORDINARY_PATH), current_user=user, db=_FakeDB(rows)
            )
            is user
        )

    def test_env_sourced_text_has_no_change_history(self, monkeypatch):
        """No stored row → nothing to compare; an .env edit needs a restart anyway."""
        monkeypatch.setattr(settings, "LOGIN_BANNER_ENABLED", True)
        user = _user(banner_acknowledged_at=datetime.now(UTC) - timedelta(days=365))

        assert (
            get_current_active_user(
                request=_request(ORDINARY_PATH), current_user=user, db=_FakeDB([])
            )
            is user
        )


# ── the gate is off unless the banner is on ──────────────────────────────────────


class TestTheGateIsOffByDefault:
    def test_a_disabled_banner_gates_nothing(self, banner_off):
        user = _user()

        assert (
            get_current_active_user(
                request=_request(ORDINARY_PATH),
                current_user=user,
                db=_FakeDB(_banner_rows(enabled=False)),
            )
            is user
        )

    def test_no_configuration_and_no_env_gates_nothing(self, banner_off):
        user = _user()

        assert (
            get_current_active_user(
                request=_request(ORDINARY_PATH), current_user=user, db=_FakeDB([])
            )
            is user
        )

    def test_the_stored_value_wins_over_the_environment(self, monkeypatch):
        """DB > .env, the same precedence GET /auth/banner uses."""
        monkeypatch.setattr(settings, "LOGIN_BANNER_ENABLED", True)
        user = _user()

        assert (
            get_current_active_user(
                request=_request(ORDINARY_PATH),
                current_user=user,
                db=_FakeDB(_banner_rows(enabled=False)),
            )
            is user
        )

    def test_an_unusable_session_degrades_to_the_environment(self, banner_off):
        """This runs on every authenticated request; it must not raise."""
        user = _user()
        broken = SimpleNamespace()  # no .query at all

        assert (
            get_current_active_user(request=_request(ORDINARY_PATH), current_user=user, db=broken)
            is user
        )

    def test_inactive_user_still_gets_the_original_400(self, banner_off):
        """The banner gate must not swallow the pre-existing deactivation check."""
        with pytest.raises(HTTPException) as exc:
            get_current_active_user(
                request=_request(), current_user=_user(is_active=False), db=_FakeDB(_banner_rows())
            )

        assert exc.value.status_code == 400
