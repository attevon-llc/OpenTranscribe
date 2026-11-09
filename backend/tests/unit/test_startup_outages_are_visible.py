"""Startup steps and the bucket-policy sweep keep going on failure but say why."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
from minio.error import S3Error

import app.startup.maintenance as maintenance
import app.startup.warmups as warmups
from app.scripts.probe_torch_thread_scope import _run_on_thread


def _s3_error(code: str) -> S3Error:
    return S3Error(MagicMock(), code, code, "res", "req", "host")


def test_missing_policy_is_the_quiet_secure_default(caplog):
    client = MagicMock()
    client.get_bucket_policy.side_effect = _s3_error("NoSuchBucketPolicy")
    with caplog.at_level(logging.DEBUG, logger=maintenance.__name__):
        maintenance._remove_public_bucket_policy(client, "media")
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_storage_outage_while_checking_public_policy_is_an_error_with_traceback(caplog):
    client = MagicMock()
    client.get_bucket_policy.side_effect = _s3_error("InternalError")
    with caplog.at_level(logging.ERROR, logger=maintenance.__name__):
        maintenance._remove_public_bucket_policy(client, "media")
    assert any(r.levelno == logging.ERROR and r.exc_info for r in caplog.records)


def test_unparseable_policy_is_an_error_with_traceback(caplog):
    client = MagicMock()
    client.get_bucket_policy.return_value = "{not json"
    with caplog.at_level(logging.ERROR, logger=maintenance.__name__):
        maintenance._remove_public_bucket_policy(client, "media")
    assert any(r.levelno == logging.ERROR and r.exc_info for r in caplog.records)


def test_bucket_policy_bug_propagates():
    client = MagicMock()
    client.get_bucket_policy.side_effect = RuntimeError("bug")
    with pytest.raises(RuntimeError):
        maintenance._remove_public_bucket_policy(client, "media")


def test_warmup_failure_is_logged_with_traceback(monkeypatch, caplog):
    import app.services.redaction.warmup as pii

    def _boom():
        raise RuntimeError("presidio missing")

    monkeypatch.setattr(pii, "start_pii_warmup", _boom)
    with caplog.at_level(logging.WARNING, logger=warmups.__name__):
        warmups._start_pii_warmup()
    assert any(r.exc_info for r in caplog.records)


def test_probe_thread_error_reaches_the_caller():
    def _boom():
        raise KeyError("from the worker thread")

    with pytest.raises(KeyError):
        _run_on_thread("probe-test", _boom)


def test_probe_thread_returns_value_on_a_fresh_thread():
    import threading

    assert _run_on_thread("probe-test", lambda: threading.current_thread().name).startswith(
        "probe-test"
    )
