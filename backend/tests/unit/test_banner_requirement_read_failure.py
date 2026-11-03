"""A failed banner-config read must not switch the AC-8 banner gate off.

``_banner_requirement`` answered a failed ``auth_config`` read with the ``.env``
value. A deployment that enabled the banner only in the admin UI (``.env`` off, the
default) therefore stopped requiring consent for as long as the read kept failing.

On a failed read the gate now also honours the last value this process read
successfully: once it has seen the banner enabled, an outage cannot turn it off.
A deployment that never enabled it is unaffected, so a read failure cannot refuse
the users of a banner-less deployment a banner nobody can show them.
"""

# mypy: disable-error-code="arg-type"
# The gate is called with structural stand-ins for the request, user and session.
from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import HTTPException

from app.api.endpoints.auth import dependencies as gates_module
from app.api.endpoints.auth.dependencies import ERROR_CODE_BANNER_ACKNOWLEDGMENT_REQUIRED
from app.core.config import settings

pytestmark = pytest.mark.unit

TEXT_CHANGED_AT = datetime(2026, 8, 13, 9, 30, tzinfo=UTC)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_args):
        return self

    def all(self):
        return self._rows


class _ReadableDB:
    """A session whose ``auth_config`` read returns the given rows."""

    def __init__(self, enabled: str):
        self._rows = [
            SimpleNamespace(
                config_key="login_banner_enabled", config_value=enabled, updated_at=None
            ),
            SimpleNamespace(
                config_key="login_banner_text",
                config_value="AUTHORIZED USE ONLY",
                updated_at=TEXT_CHANGED_AT,
            ),
        ]

    def query(self, _model):
        return _Rows(self._rows)


class _FailingDB:
    """A session whose every query raises, as during a database outage."""

    def query(self, _model):
        raise RuntimeError("connection reset")


def _request(path: str):
    return SimpleNamespace(scope={"route": SimpleNamespace(path=path)})


def _user(acknowledged_at: datetime | None = None):
    return SimpleNamespace(id=7, banner_acknowledged_at=acknowledged_at)


@pytest.fixture(autouse=True)
def fresh_process(monkeypatch):
    """No successful read yet in this process, and ``.env`` leaves the banner off."""
    monkeypatch.setattr(gates_module, "_last_banner_read", None, raising=False)
    monkeypatch.setattr(settings, "LOGIN_BANNER_ENABLED", False)


class TestTheRequirement:
    def test_a_failed_read_after_seeing_it_enabled_still_requires_the_banner(self):
        assert gates_module._banner_requirement(_ReadableDB("true")) == (True, TEXT_CHANGED_AT)

        assert gates_module._banner_requirement(_FailingDB()) == (True, TEXT_CHANGED_AT)

    def test_a_failed_read_after_seeing_it_disabled_follows_the_environment(self):
        """Control: a deployment whose stored value is off stays off."""
        assert gates_module._banner_requirement(_ReadableDB("false"))[0] is False

        assert gates_module._banner_requirement(_FailingDB()) == (False, None)

    def test_a_failed_read_with_no_earlier_read_follows_the_environment(self):
        """Control: nothing known yet, so a banner-less deployment is not refused."""
        assert gates_module._banner_requirement(_FailingDB()) == (False, None)

    def test_the_environment_still_enables_it_on_a_failed_read(self, monkeypatch):
        monkeypatch.setattr(settings, "LOGIN_BANNER_ENABLED", True)
        assert gates_module._banner_requirement(_ReadableDB("false"))[0] is False

        assert gates_module._banner_requirement(_FailingDB()) == (True, None)


class TestTheGate:
    def test_an_unacknowledged_caller_is_refused_during_the_failure(self):
        gates_module._banner_requirement(_ReadableDB("true"))

        with pytest.raises(HTTPException) as caught:
            gates_module._enforce_banner_acknowledgment(
                _user(), _request(f"{settings.API_PREFIX}/files"), _FailingDB()
            )

        assert caught.value.status_code == 403
        assert cast(dict, caught.value.detail)["code"] == ERROR_CODE_BANNER_ACKNOWLEDGMENT_REQUIRED

    def test_the_acknowledge_route_stays_reachable_during_the_failure(self):
        """The remedy is exempt, so the gate can always be cleared."""
        gates_module._banner_requirement(_ReadableDB("true"))
        user = _user()

        gates_module._enforce_banner_acknowledgment(
            user, _request(f"{settings.API_PREFIX}/auth/banner/acknowledge"), _FailingDB()
        )

        assert user.banner_acknowledged_at is None

    def test_a_caller_who_acknowledged_the_current_text_passes_during_the_failure(self):
        gates_module._banner_requirement(_ReadableDB("true"))
        user = _user(acknowledged_at=TEXT_CHANGED_AT + timedelta(minutes=1))

        gates_module._enforce_banner_acknowledgment(
            user, _request(f"{settings.API_PREFIX}/files"), _FailingDB()
        )

        assert user.banner_acknowledged_at > TEXT_CHANGED_AT
