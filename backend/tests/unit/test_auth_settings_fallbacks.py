"""The auth-settings resolver's fallbacks: what a bad or missing value turns into.

``DynamicAuthSettings`` feeds every auth control (lockout, password policy, sessions,
PKI), so how it handles a value it cannot use decides whether a misconfiguration
fails safe. Each test below pins one fallback that no other test asserted: a
mutation that changed it left the whole auth suite green.

* a value that is not an integer resolves to the caller's default, not to ``0``
  (``0`` disables several controls, e.g. ``max_concurrent_sessions``);
* a string flag is parsed strictly: ``"false"`` is False, not "non-empty, so True";
* a database error falls back to the ``.env`` value instead of raising, so a DB
  hiccup cannot turn a login into a 500;
* a failed post-commit prime drops the process-wide cache rather than leaving the
  previous value enforced with no expiry under ``TESTING`` (30 s in production).
"""

from __future__ import annotations

from typing import cast
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.core.auth_settings import DynamicAuthSettings
from app.core.config import settings
from app.core.process_auth_settings import get_process_auth_settings
from app.core.process_auth_settings import prime_process_auth_settings
from app.core.process_auth_settings import publish_process_auth_setting
from app.services.auth_config_service import AuthConfigService

_FAKE_SESSION = cast(Session, object())


def _resolver() -> DynamicAuthSettings:
    """An uncached resolver whose database layer is whatever the test patches in."""
    return DynamicAuthSettings(db=_FAKE_SESSION, enable_cache=False)


@pytest.mark.parametrize("raw", ["abc", "12.5", "", [], {}])
def test_a_non_integer_value_resolves_to_the_callers_default(raw: object) -> None:
    with patch.object(AuthConfigService, "get_effective_config", return_value=raw):
        assert _resolver().get_int("account_lockout_threshold", 7) == 7


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("false", False), ("0", False), ("off", False), ("no", False), ("true", True), ("ON", True)],
)
def test_a_string_flag_is_parsed_not_tested_for_emptiness(raw: str, expected: bool) -> None:
    with patch.object(AuthConfigService, "get_effective_config", return_value=raw):
        assert _resolver().get_bool("account_lockout_enabled", not expected) is expected


def test_a_database_error_falls_back_to_the_env_value() -> None:
    with patch.object(
        AuthConfigService, "get_effective_config", side_effect=RuntimeError("db down")
    ):
        resolved = _resolver().account_lockout_threshold
    assert resolved == settings.ACCOUNT_LOCKOUT_THRESHOLD


def test_a_failed_prime_drops_the_process_cache() -> None:
    stale = settings.ACCOUNT_LOCKOUT_THRESHOLD + 40
    publish_process_auth_setting("account_lockout_threshold", stale)
    assert get_process_auth_settings().account_lockout_threshold == stale

    with patch.object(
        AuthConfigService, "get_effective_config", side_effect=RuntimeError("db down")
    ):
        prime_process_auth_settings(_FAKE_SESSION, "account_lockout_threshold")

    assert (
        get_process_auth_settings().account_lockout_threshold == settings.ACCOUNT_LOCKOUT_THRESHOLD
    )
