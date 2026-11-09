"""B4 broad-except lane: outages must be visible, and only the documented errors are absorbed."""

from __future__ import annotations

import logging
import sys
import types
from unittest.mock import MagicMock

import pytest
from opensearchpy.exceptions import ConnectionError as OSConnectionError
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError

from app.transcription import cuda_health


def _has_traceback(caplog, fragment: str) -> bool:
    return any(fragment in r.getMessage() and r.exc_info for r in caplog.records)


def test_poisoned_requeue_redis_outage_fails_open_with_traceback(monkeypatch, caplog):
    from app.tasks.transcription import context

    client = MagicMock()
    client.incr.side_effect = RedisConnectionError("down")
    monkeypatch.setattr(context.redis_core, "get_redis", lambda: client)
    with caplog.at_level(logging.WARNING):
        assert context._poisoned_requeue_allowed("t1") is True
    assert _has_traceback(caplog, "poisoned-context requeues")


def test_poisoned_requeue_programming_error_propagates(monkeypatch):
    from app.tasks.transcription import context

    client = MagicMock()
    client.incr.side_effect = TypeError("bug")
    monkeypatch.setattr(context.redis_core, "get_redis", lambda: client)
    with pytest.raises(TypeError):
        context._poisoned_requeue_allowed("t1")


def test_gpu_split_claim_redis_outage_fails_open_with_traceback(monkeypatch, caplog):
    from app.core import redis as redis_mod
    from app.tasks.transcription.core import _claim_gpu_split_diarize_dispatch

    client = MagicMock()
    client.set.side_effect = RedisConnectionError("down")
    monkeypatch.setattr(redis_mod, "get_redis", lambda: client)
    with caplog.at_level(logging.WARNING):
        assert _claim_gpu_split_diarize_dispatch("t2") is True
    assert _has_traceback(caplog, "dispatch claim failed")


def test_gpu_split_claim_programming_error_propagates(monkeypatch):
    from app.core import redis as redis_mod
    from app.tasks.transcription.core import _claim_gpu_split_diarize_dispatch

    client = MagicMock()
    client.set.side_effect = TypeError("bug")
    monkeypatch.setattr(redis_mod, "get_redis", lambda: client)
    with pytest.raises(TypeError):
        _claim_gpu_split_diarize_dispatch("t2")


def _fake_torch(monkeypatch, *, available=True, ones=None):
    cuda = types.SimpleNamespace(
        is_available=MagicMock(side_effect=None) if available is not None else None,
        synchronize=lambda i: None,
    )
    torch = types.SimpleNamespace(cuda=cuda, ones=ones or (lambda *a, **k: MagicMock()))
    monkeypatch.setitem(sys.modules, "torch", torch)
    return torch


def test_cuda_probe_failure_logs_traceback(monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("illegal memory access")

    _fake_torch(monkeypatch, ones=boom)
    with caplog.at_level(logging.ERROR):
        assert cuda_health.cuda_context_healthy(0) is False
    assert _has_traceback(caplog, "CUDA context probe failed")


def test_cuda_availability_runtime_error_is_unknown_healthy_with_traceback(monkeypatch, caplog):
    torch = _fake_torch(monkeypatch)
    torch.cuda.is_available.side_effect = RuntimeError("driver")
    with caplog.at_level(logging.WARNING):
        assert cuda_health.cuda_context_healthy(0) is True
    assert _has_traceback(caplog, "CUDA availability")


def test_cuda_availability_unexpected_error_propagates(monkeypatch):
    torch = _fake_torch(monkeypatch)
    torch.cuda.is_available.side_effect = TypeError("bug")
    with pytest.raises(TypeError):
        cuda_health.cuda_context_healthy(0)


def test_collection_prompt_db_outage_is_not_no_prompt(monkeypatch, caplog):
    from app.tasks.transcription import downstream

    def broken():
        raise OperationalError("select", {}, Exception("db down"))

    monkeypatch.setattr("app.db.session_utils.session_scope", broken)
    with caplog.at_level(logging.ERROR), pytest.raises(OperationalError):
        downstream._get_collection_prompt_uuid(7)
    assert _has_traceback(caplog, "collection prompt")


def test_embedding_compat_opensearch_outage_logged_with_traceback(monkeypatch, caplog):
    from app.services.embedding_mode_service import EmbeddingModeService
    from app.tasks.transcription import embeddings

    def down():
        raise OSConnectionError("N/A", "down", Exception("x"))

    monkeypatch.setattr(EmbeddingModeService, "get_embedding_dimension", staticmethod(down))
    with caplog.at_level(logging.ERROR):
        assert (
            embeddings._should_use_native_embeddings({"native_speaker_embeddings": {"a": [0.0]}})
            is False
        )
    assert _has_traceback(caplog, "embedding dimension compatibility")


def test_embedding_compat_programming_error_propagates(monkeypatch):
    from app.services.embedding_mode_service import EmbeddingModeService
    from app.tasks.transcription import embeddings

    def bug():
        raise TypeError("bug")

    monkeypatch.setattr(EmbeddingModeService, "get_embedding_dimension", staticmethod(bug))
    with pytest.raises(TypeError):
        embeddings._should_use_native_embeddings({"native_speaker_embeddings": {"a": [0.0]}})
