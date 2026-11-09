"""Outage injection for the narrowed broad-except handlers in ``app.auth`` / ``app.core``.

Each handler used to ``except Exception`` and carry on, so an outage and a programming
error looked identical and neither left a traceback. They now catch only the library's
own error family, log with ``exc_info``, and let anything else propagate.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
import redis

from app.api import rate_limit
from app.auth import lockout_store
from app.auth import mfa
from app.auth import password_history
from app.auth import pki_ocsp
from app.auth import session as auth_session
from app.core import task_liveness
from app.core import task_replay


def _has_traceback(caplog: pytest.LogCaptureFixture, fragment: str) -> bool:
    return any(fragment in r.getMessage() and r.exc_info for r in caplog.records)


def test_current_password_unreadable_hash_is_not_a_match_and_keeps_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    user = MagicMock(hashed_password="EXTERNAL_AUTH_NO_PASSWORD")
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = user
    with (
        caplog.at_level(logging.DEBUG, logger=password_history.logger.name),
        patch.object(password_history, "verify_password", side_effect=ValueError("unknown hash")),
    ):
        assert password_history._matches_current_password(db, 1, "pw") is False
    assert _has_traceback(caplog, "stored hash is unreadable")


def test_current_password_programming_error_propagates() -> None:
    user = MagicMock(hashed_password="x")
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = user
    with patch.object(password_history, "verify_password", side_effect=RuntimeError("bug")):
        with pytest.raises(RuntimeError):
            password_history._matches_current_password(db, 1, "pw")


def test_totp_replay_guard_fails_closed_on_redis_outage_when_required(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "MFA_REQUIRE_REDIS", True)
    with (
        caplog.at_level(logging.ERROR, logger=mfa.logger.name),
        patch(
            "app.core.redis.get_request_redis",
            side_effect=redis.ConnectionError("down"),
        ),
    ):
        assert mfa.MFAService._consume_totp_code(1, "123456") is False
    assert _has_traceback(caplog, "replay protection unavailable")


def test_totp_replay_guard_does_not_swallow_a_bug(monkeypatch: pytest.MonkeyPatch) -> None:
    with patch("app.core.redis.get_request_redis", side_effect=RuntimeError("bug")):
        with pytest.raises(RuntimeError):
            mfa.MFAService._consume_totp_code(1, "123456")


def test_lockout_store_redis_outage_degrades_to_memory_with_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        caplog.at_level(logging.WARNING, logger=lockout_store.logger.name),
        patch.object(
            lockout_store.redis.Redis, "from_url", side_effect=redis.ConnectionError("down")
        ),
    ):
        assert lockout_store._get_redis_client() is None
    assert _has_traceback(caplog, "Redis connection failed for lockout storage")


def test_lockout_store_bug_propagates() -> None:
    with patch.object(lockout_store.redis.Redis, "from_url", side_effect=RuntimeError("bug")):
        with pytest.raises(RuntimeError):
            lockout_store._get_redis_client()


def test_session_redis_outage_returns_none_with_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        caplog.at_level(logging.WARNING, logger=auth_session.logger.name),
        patch.object(
            auth_session.redis.Redis, "from_url", side_effect=redis.ConnectionError("down")
        ),
    ):
        assert auth_session.get_redis_client() is None
    assert _has_traceback(caplog, "Redis connection failed")


def test_rate_limit_startup_probe_reports_outage_with_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        caplog.at_level(logging.WARNING, logger=rate_limit.logger.name),
        patch.object(rate_limit.redis, "from_url", side_effect=redis.ConnectionError("down")),
    ):
        assert rate_limit._redis_reachable() is False
    assert _has_traceback(caplog, "Redis unreachable at rate-limiter startup")


def test_ocsp_request_build_failure_is_logged_with_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        caplog.at_level(logging.ERROR, logger=pki_ocsp.logger.name),
        patch.object(
            pki_ocsp.ocsp.OCSPRequestBuilder,
            "add_certificate",
            side_effect=ValueError("bad cert"),
        ),
    ):
        assert pki_ocsp._build_ocsp_request(MagicMock(), MagicMock()) is None
    assert _has_traceback(caplog, "Failed to build OCSP request")


def test_ocsp_request_build_bug_propagates() -> None:
    with patch.object(
        pki_ocsp.ocsp.OCSPRequestBuilder, "add_certificate", side_effect=RuntimeError("bug")
    ):
        with pytest.raises(RuntimeError):
            pki_ocsp._build_ocsp_request(MagicMock(), MagicMock())


def test_liveness_unreadable_redis_reads_unknown_with_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        caplog.at_level(logging.WARNING, logger=task_liveness.logger.name),
        patch("app.core.redis.get_request_redis", side_effect=redis.ConnectionError("down")),
    ):
        runs = task_liveness.probe_runs(["t1"])
    assert runs["t1"].state == task_liveness.RunState.UNKNOWN
    assert _has_traceback(caplog, "Could not read transcription liveness")


def test_liveness_bug_propagates() -> None:
    with patch("app.core.redis.get_request_redis", side_effect=RuntimeError("bug")):
        with pytest.raises(RuntimeError):
            task_liveness.probe_runs(["t1"])


def test_replay_is_alive_unreadable_redis_is_none_with_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        caplog.at_level(logging.WARNING, logger=task_replay.logger.name),
        patch.object(
            task_replay.redis_core,
            "get_request_redis",
            side_effect=redis.ConnectionError("down"),
        ),
    ):
        assert task_replay.is_alive("t1") is None
    assert _has_traceback(caplog, "Could not read the heartbeat")
