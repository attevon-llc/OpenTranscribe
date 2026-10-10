"""``Retry-After`` must express time-to-reset, not the configured window length.

Issue #788 (surface ``Retry-After`` in the UI) audited the one 429 handler that
already sent the header and found it computed the WRONG number:
``_extract_retry_seconds`` string-matched the window unit ("minute"/"hour"/...)
out of the limit's description and returned a fixed constant per unit — always
60 for any "N per minute" limit, no matter how much of the window had already
elapsed. A client tripping a 10/minute limit at second 57 was told to wait 60
seconds when the true answer was ~3.

``_retry_after_from_window_stats`` fixes this by reading the real reset time off
the limiter's own storage, using ``request.state.view_rate_limit`` — the
``(limit_item, identifiers)`` pair slowapi's ``__evaluate_limits`` sets
immediately before raising ``RateLimitExceeded``, i.e. the exact bucket that was
just hit.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import limits
import pytest
from slowapi.errors import RateLimitExceeded
from slowapi.wrappers import Limit
from starlette.requests import Request

from app.auth import rate_limit as rate_limit_module
from app.auth.rate_limit import _extract_retry_seconds
from app.auth.rate_limit import _retry_after_from_window_stats
from app.auth.rate_limit import rate_limit_exceeded_handler


def _make_request() -> Request:
    """A minimal real Starlette Request — enough for ``resolve_client_ip`` and
    ``request.state`` to work without spinning up the full app."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/some-endpoint",
        "headers": [],
        "client": ("203.0.113.5", 12345),
    }
    return Request(scope)


def _make_exc(limit_string: str) -> RateLimitExceeded:
    """Build a real ``RateLimitExceeded`` the way slowapi does, so ``exc.detail``
    is the same limit-description string the production code sees."""
    item = limits.parse(limit_string)
    limit = Limit(
        limit=item,
        key_func=lambda request: "irrelevant",
        scope=None,
        per_method=False,
        methods=None,
        error_message=None,
        exempt_when=None,
        cost=1,
        override_defaults=False,
    )
    return RateLimitExceeded(limit)


class TestExtractRetrySecondsIsAWindowLengthNotAResetTime:
    """Pin the OLD function's documented behaviour so nobody "fixes" it in place
    and silently changes the fallback contract — it is explicitly window length."""

    def test_a_ten_per_minute_limit_always_reports_sixty(self):
        # Whether the caller is 1 second or 59 seconds into the window, this
        # function has no way to know and always answers the same constant.
        assert _extract_retry_seconds("10 per 1 minute") == 60

    def test_a_per_second_limit_reports_one_regardless_of_the_configured_count(self):
        # e.g. "5 per 3 second" — the window is actually 3 seconds, not 1.
        assert _extract_retry_seconds("5 per 3 second") == 1


class TestRetryAfterFromWindowStatsUsesTheRealResetTime:
    def test_returns_none_when_slowapi_state_is_absent(self):
        request = _make_request()
        exc = _make_exc("10 per 1 minute")

        assert _retry_after_from_window_stats(request, exc) is None

    def test_reads_the_actual_reset_time_off_the_limiter_storage(self):
        """The core proof: with slowapi's per-request state present, the answer
        comes from the storage's real expiry — not a per-unit constant."""
        request = _make_request()
        exc = _make_exc("10 per 1 minute")
        limit_item = exc.limit.limit
        identifiers = ["some-key", "some-scope"]
        request.state.view_rate_limit = (limit_item, identifiers)

        # The bucket is 3 seconds from resetting — nowhere near the 60s a
        # "per minute" window's fallback would report.
        fake_reset_at = time.time() + 3
        with patch.object(
            rate_limit_module.limiter.limiter,
            "get_window_stats",
            return_value=(fake_reset_at, 0),
        ) as mocked:
            seconds = _retry_after_from_window_stats(request, exc)

        mocked.assert_called_once_with(limit_item, *identifiers)
        assert seconds is not None
        # Allow for the wall-clock tick between computing fake_reset_at and the
        # function's own time.time() call.
        assert 1 <= seconds <= 3

    def test_falls_back_to_none_on_a_storage_error(self):
        request = _make_request()
        exc = _make_exc("10 per 1 minute")
        request.state.view_rate_limit = (exc.limit.limit, ["k", "s"])

        with patch.object(
            rate_limit_module.limiter.limiter,
            "get_window_stats",
            side_effect=RuntimeError("storage unreachable"),
        ):
            assert _retry_after_from_window_stats(request, exc) is None


class TestRateLimitExceededHandlerPrefersTheRealResetTime:
    """The end-to-end proof — this is what a client actually receives.

    RED against the code before this fix: the handler called
    ``_extract_retry_seconds(exc.detail)`` unconditionally and always answered
    "60" for a "10 per 1 minute" limit, even with slowapi's window-stats state
    present and reporting 3 seconds to reset.
    """

    def test_header_reflects_the_real_reset_time_not_the_window_length(self):
        request = _make_request()
        exc = _make_exc("10 per 1 minute")
        limit_item = exc.limit.limit
        request.state.view_rate_limit = (limit_item, ["some-key", "some-scope"])

        fake_reset_at = time.time() + 3
        with patch.object(
            rate_limit_module.limiter.limiter,
            "get_window_stats",
            return_value=(fake_reset_at, 0),
        ):
            response = rate_limit_exceeded_handler(request, exc)

        retry_after = int(response.headers["Retry-After"])
        assert 1 <= retry_after <= 3, (
            f"expected the real ~3s reset time, got {retry_after} "
            "(60 would mean the handler fell back to the window-length parse)"
        )

    def test_falls_back_to_window_length_when_state_is_absent(self):
        """Without slowapi's state, the handler must still send SOMETHING —
        never drop the header outright."""
        request = _make_request()
        exc = _make_exc("10 per 1 minute")

        response = rate_limit_exceeded_handler(request, exc)

        assert response.headers["Retry-After"] == "60"


@pytest.mark.parametrize(
    ("limit_string", "expected_reset_seconds"),
    [
        ("10 per 1 minute", 2),
        ("3 per 1 hour", 45),
    ],
)
def test_window_stats_path_is_independent_of_the_configured_unit(
    limit_string, expected_reset_seconds
):
    """The real-reset-time path doesn't care whether the window is a minute or
    an hour — it just reads storage. The window-length fallback DOES care, and
    would report 60 or 3600 respectively, neither of which is the real answer."""
    request = _make_request()
    exc = _make_exc(limit_string)
    limit_item = exc.limit.limit
    request.state.view_rate_limit = (limit_item, ["k", "s"])

    fake_reset_at = time.time() + expected_reset_seconds
    with patch.object(
        rate_limit_module.limiter.limiter,
        "get_window_stats",
        return_value=(fake_reset_at, 0),
    ):
        seconds = _retry_after_from_window_stats(request, exc)

    assert seconds is not None
    assert abs(seconds - expected_reset_seconds) <= 1
