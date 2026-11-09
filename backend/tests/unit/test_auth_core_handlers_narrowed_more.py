"""Outage injection, part 2: Redis, broker and cryptography handlers in auth and core.

For each handler an outage of the kind its library raises is absorbed with a traceback
in the log (the documented soft direction), and an unrelated bug is not swallowed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
import redis
from cryptography.exceptions import UnsupportedAlgorithm

from app.auth import mfa
from app.auth import pki_certificate
from app.auth import pki_revocation
from app.auth.token_service import TokenService
from app.core import broker_orphans
from app.core import task_cancellation
from app.core import task_liveness
from app.core import task_replay
from app.core import worker_metrics

REDIS_DOWN = redis.ConnectionError("down")
GETTER = "app.core.redis.get_request_redis"


def _logged_with_traceback(caplog: pytest.LogCaptureFixture, fragment: str) -> bool:
    return any(fragment in r.getMessage() and r.exc_info for r in caplog.records)


# (call, patch target, log fragment, expected return)
REDIS_SOFT: list[tuple[str, Callable[[], Any], str, Any]] = [
    (
        "cancel_requested",
        lambda: task_cancellation.cancel_requested("t"),
        "cancellation flag",
        False,
    ),
    ("clear_cancel", lambda: task_cancellation.clear_cancel("t"), "clear the cancellation", None),
    (
        "request_cancel",
        lambda: task_cancellation.request_cancel("t", "f"),
        "cooperative cancellation",
        False,
    ),
    ("mark_queued", lambda: task_liveness.mark_queued("t"), "as queued", None),
    ("clear_markers", lambda: task_liveness.clear_run_markers("t"), "liveness markers", None),
    (
        "record_requeue",
        lambda: task_liveness.record_infra_requeue("f"),
        "infrastructure requeue",
        None,
    ),
    (
        "clear_requeues",
        lambda: task_liveness.clear_infra_requeues("f"),
        "infrastructure requeues",
        None,
    ),
    ("list_requeued", lambda: task_liveness.files_requeued_at_least(3), "requeued files", None),
]


@pytest.mark.parametrize(
    ("call", "fragment", "expected"),
    [pytest.param(c, f, e, id=i) for i, c, f, e in REDIS_SOFT],
)
def test_redis_outage_is_absorbed_with_a_traceback(
    call: Callable[[], Any], fragment: str, expected: Any, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING), patch(GETTER, side_effect=REDIS_DOWN):
        assert call() == expected
    assert _logged_with_traceback(caplog, fragment)


@pytest.mark.parametrize(
    "call",
    [pytest.param(c, id=i) for i, c, _f, _e in REDIS_SOFT if i != "request_cancel"],
)
def test_redis_handlers_do_not_swallow_a_bug(call: Callable[[], Any]) -> None:
    with patch(GETTER, side_effect=RuntimeError("bug")), pytest.raises(RuntimeError):
        call()


def test_replay_sweep_reports_unreadable_redis_and_keeps_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        caplog.at_level(logging.WARNING),
        patch.object(task_replay.redis_core, "get_request_redis", side_effect=REDIS_DOWN),
    ):
        summary = task_replay.reclaim_lost_tasks(send_task=MagicMock())
    assert summary["error"] == 1
    assert _logged_with_traceback(caplog, "Redis unreadable")


def test_replay_tracked_ids_empty_on_outage_and_propagates_bugs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        caplog.at_level(logging.WARNING),
        patch.object(task_replay.redis_core, "get_request_redis", side_effect=REDIS_DOWN),
    ):
        assert task_replay.replay_tracked_ids(["a"]) == set()
    assert _logged_with_traceback(caplog, "Could not read replay state")
    with (
        patch.object(task_replay.redis_core, "get_request_redis", side_effect=RuntimeError("bug")),
        pytest.raises(RuntimeError),
    ):
        task_replay.replay_tracked_ids(["a"])


def test_broker_unreadable_queues_are_none_with_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        caplog.at_level(logging.WARNING),
        patch.object(broker_orphans, "_broker_connection", side_effect=OSError("refused")),
    ):
        assert broker_orphans.queued_run_ids() is None
    assert _logged_with_traceback(caplog, "Could not read the queued stages")


def test_broker_bug_propagates() -> None:
    with (
        patch.object(broker_orphans, "_broker_connection", side_effect=RuntimeError("bug")),
        pytest.raises(RuntimeError),
    ):
        broker_orphans.queued_run_ids()


def test_task_label_falls_back_when_the_registry_is_not_a_mapping() -> None:
    class Task:
        name = "x"
        app = MagicMock(tasks=None)

    assert worker_metrics.task_label(Task()) == worker_metrics.OTHER_TASK


def test_revoked_token_lookup_falls_back_to_database_on_redis_outage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = TokenService()
    store = MagicMock()
    store.get.side_effect = redis.ConnectionError("down")
    with (
        caplog.at_level(logging.CRITICAL),
        patch.object(TokenService, "store", store),
        patch("app.auth.token_service.is_revoked_from_database", return_value=True) as fallback,
    ):
        assert service.is_token_revoked("jti-12345678", db=MagicMock()) is True
    fallback.assert_called_once()
    assert _logged_with_traceback(caplog, "Redis lookup failed")


def test_revoked_token_lookup_bug_propagates() -> None:
    service = TokenService()
    store = MagicMock()
    store.get.side_effect = RuntimeError("bug")
    with patch.object(TokenService, "store", store), pytest.raises(RuntimeError):
        service.is_token_revoked("jti-12345678", db=MagicMock())


def test_certificate_parse_failure_is_none_with_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR):
        assert pki_certificate.parse_certificate("not a pem") is None
    assert _logged_with_traceback(caplog, "Failed to parse certificate")


def test_certificate_parse_unsupported_algorithm_is_none() -> None:
    with patch.object(
        pki_certificate.x509, "load_pem_x509_certificate", side_effect=UnsupportedAlgorithm("x")
    ):
        assert pki_certificate.parse_certificate("x") is None


def test_certificate_parse_bug_propagates() -> None:
    with (
        patch.object(
            pki_certificate.x509, "load_pem_x509_certificate", side_effect=RuntimeError("bug")
        ),
        pytest.raises(RuntimeError),
    ):
        pki_certificate.parse_certificate("x")


def test_ca_certificate_unreadable_is_none_with_traceback(
    tmp_path: Any, caplog: pytest.LogCaptureFixture
) -> None:
    bad = tmp_path / "ca.pem"
    bad.write_text("garbage")
    settings = MagicMock(pki_ca_cert_path=str(bad))
    with (
        caplog.at_level(logging.ERROR),
        patch.object(pki_revocation, "get_process_auth_settings", return_value=settings),
    ):
        assert pki_revocation._load_issuer_certificate() is None
    assert _logged_with_traceback(caplog, "Failed to load CA certificate")


def test_unparseable_crl_is_none_with_traceback(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        assert pki_revocation._parse_and_verify_crl(b"not a crl", "http://crl", MagicMock()) is None
    assert _logged_with_traceback(caplog, "Failed to parse CRL")


def test_backup_code_with_corrupt_hash_is_skipped_with_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=mfa.logger.name):
        assert mfa.MFAService.verify_backup_code("AAAA-BBBB", ["not-a-hash"]) == (False, None)
    assert _logged_with_traceback(caplog, "Backup code hash could not be verified")


def test_totp_malformed_secret_is_refused_with_traceback(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR, logger=mfa.logger.name):
        assert mfa.MFAService.verify_totp("!!not base32!!", "123456") is False
    assert _logged_with_traceback(caplog, "TOTP verification error")
