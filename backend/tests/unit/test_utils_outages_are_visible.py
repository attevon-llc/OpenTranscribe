"""Outage-injection tests for ``app.utils``: a failure is logged with its traceback, the
documented soft/closed direction holds, and a programming error is NOT swallowed.

Each ``except Exception`` here used to turn every bug into the soft value (``False``, ``{}``,
``True`` ...). The one that mattered most: ``is_file_safe_to_delete`` said "safe to delete"
whenever the Celery result backend could not be read.
"""

from __future__ import annotations

import logging
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from redis.exceptions import RedisError
from sqlalchemy.orm import Session

import app.utils.benchmark_timing as bt
import app.utils.boot_once as boot_once
import app.utils.file_retry as file_retry
import app.utils.gpu_stats as gpu_stats
import app.utils.websocket_notify as ws_notify


def _traceback_logged(caplog, name: str) -> bool:
    return any(r.name == name and r.exc_info for r in caplog.records)


class _StateRaises:
    def __init__(self, exc: Exception):
        self._exc = exc

    def __call__(self, _task_id):
        outer = self

        class _Result:
            @property
            def state(self):
                raise outer._exc

        return _Result()


def _file_with_task(monkeypatch):
    mf = SimpleNamespace(active_task_id="t-1", status=None)
    monkeypatch.setattr(file_retry, "get_refreshed_object", lambda *_a, **_k: mf)


def test_unreadable_result_backend_means_not_safe_to_delete(monkeypatch, caplog):
    _file_with_task(monkeypatch)
    monkeypatch.setattr(file_retry, "AsyncResult", _StateRaises(RedisError("backend down")))
    with caplog.at_level(logging.WARNING, logger=file_retry.__name__):
        safe, reason = file_retry.is_file_safe_to_delete(Mock(spec=Session), 1)
    assert safe is False
    assert "t-1" in reason
    assert _traceback_logged(caplog, file_retry.__name__)


def test_programming_error_in_task_check_propagates(monkeypatch):
    _file_with_task(monkeypatch)
    monkeypatch.setattr(file_retry, "AsyncResult", _StateRaises(TypeError("bug")))
    with pytest.raises(TypeError):
        file_retry.is_file_safe_to_delete(Mock(spec=Session), 1)


def test_boot_election_redis_outage_runs_step_and_logs_traceback(monkeypatch, caplog):
    import app.core.redis as core_redis

    def _down():
        raise RedisError("down")

    monkeypatch.setattr(core_redis, "get_redis", _down)
    with caplog.at_level(logging.WARNING, logger=boot_once.__name__):
        assert boot_once.run_once_per_boot("step-x") is True
    assert _traceback_logged(caplog, boot_once.__name__)


def test_boot_election_bug_propagates(monkeypatch):
    import app.core.redis as core_redis

    def _bug():
        raise RuntimeError("bug")

    monkeypatch.setattr(core_redis, "get_redis", _bug)
    with pytest.raises(RuntimeError):
        boot_once.run_once_per_boot("step-y")


def test_publish_redis_outage_returns_false_with_traceback(monkeypatch, caplog):
    class _Down:
        def publish(self, *_a):
            raise RedisError("down")

    monkeypatch.setattr(ws_notify, "get_request_redis", lambda: _Down())
    with caplog.at_level(logging.ERROR, logger=ws_notify.__name__):
        assert ws_notify._publish({"a": 1}, "user-1", "t") is False
    assert _traceback_logged(caplog, ws_notify.__name__)


def test_publish_bug_propagates(monkeypatch):
    class _Bug:
        def publish(self, *_a):
            raise RuntimeError("bug")

    monkeypatch.setattr(ws_notify, "get_request_redis", lambda: _Bug())
    with pytest.raises(RuntimeError):
        ws_notify._publish({"a": 1}, "user-1", "t")


def test_fetch_all_redis_outage_logs_warning_with_traceback(monkeypatch, caplog):
    import app.core.redis as core_redis

    class _Down:
        def hgetall(self, _key):
            raise RedisError("down")

    monkeypatch.setattr(core_redis, "get_redis", lambda: _Down())
    with caplog.at_level(logging.WARNING, logger=bt.__name__):
        assert bt.fetch_all("task-1") == {}
    assert _traceback_logged(caplog, bt.__name__)


def test_fetch_all_bug_propagates(monkeypatch):
    import app.core.redis as core_redis

    class _Bug:
        def hgetall(self, _key):
            raise RuntimeError("bug")

    monkeypatch.setattr(core_redis, "get_redis", lambda: _Bug())
    with pytest.raises(RuntimeError):
        bt.fetch_all("task-1")


def test_nvidia_smi_timeout_is_none_with_traceback(monkeypatch, caplog):
    def _timeout(*_a, **_k):
        raise subprocess.TimeoutExpired("nvidia-smi", 5)

    monkeypatch.setattr(gpu_stats.subprocess, "run", _timeout)
    with caplog.at_level(logging.WARNING, logger=gpu_stats.__name__):
        assert gpu_stats.query_gpu_stats(0) is None
    assert _traceback_logged(caplog, gpu_stats.__name__)


def test_nvidia_smi_bug_propagates(monkeypatch):
    def _bug(*_a, **_k):
        raise RuntimeError("bug")

    monkeypatch.setattr(gpu_stats.subprocess, "run", _bug)
    with pytest.raises(RuntimeError):
        gpu_stats.query_gpu_stats(0)
