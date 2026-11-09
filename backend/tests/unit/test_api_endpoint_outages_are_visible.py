"""An outage behind an API endpoint must be visible, not read as "no data".

Each test injects the dependency failure the handler used to swallow and asserts
the caller can tell it apart from a legitimate empty answer, and that the
traceback reached the log.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from redis.exceptions import RedisError


def _traceback_logged(caplog: pytest.LogCaptureFixture, logger_name: str) -> bool:
    return any(r.name == logger_name and r.exc_info for r in caplog.records)


def test_admin_timing_redis_outage_is_503_not_404(monkeypatch, caplog):
    import app.core.redis as core_redis
    from app.api.endpoints import admin_timing

    class _Down:
        def hgetall(self, _key):
            raise RedisError("down")

    monkeypatch.setattr(core_redis, "get_redis", lambda: _Down())
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    with caplog.at_level(logging.ERROR, logger=admin_timing.__name__):
        with pytest.raises(HTTPException) as exc:
            admin_timing.get_timing("t-1", current_user=MagicMock(), db=db)
    assert exc.value.status_code == 503
    assert _traceback_logged(caplog, admin_timing.__name__)


def test_admin_timing_genuine_miss_is_still_404(monkeypatch):
    import app.core.redis as core_redis
    from app.api.endpoints import admin_timing

    class _Empty:
        def hgetall(self, _key):
            return {}

    monkeypatch.setattr(core_redis, "get_redis", lambda: _Empty())
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    with pytest.raises(HTTPException) as exc:
        admin_timing.get_timing("t-1", current_user=MagicMock(), db=db)
    assert exc.value.status_code == 404


def test_revoke_batch_tasks_redis_outage_is_reported_not_swallowed(caplog):
    from app.api.endpoints import migration_stop

    class _Down:
        def get(self, _key):
            raise RedisError("down")

    with caplog.at_level(logging.WARNING, logger=migration_stop.__name__):
        revoked, failed = migration_stop.revoke_batch_tasks(_Down(), "k")
    assert (revoked, failed) == (0, True)
    assert _traceback_logged(caplog, migration_stop.__name__)


def test_revoke_batch_tasks_nothing_stored_is_not_a_failure():
    from app.api.endpoints import migration_stop

    class _Empty:
        def get(self, _key):
            return None

    assert migration_stop.revoke_batch_tasks(_Empty(), "k") == (0, False)


def test_revoke_batch_tasks_bug_propagates():
    from app.api.endpoints import migration_stop

    class _Bug:
        def get(self, _key):
            raise AttributeError("bug")

    with pytest.raises(AttributeError):
        migration_stop.revoke_batch_tasks(_Bug(), "k")


def test_ground_truth_status_outage_raises_instead_of_not_stalled(monkeypatch):
    from opensearchpy.exceptions import ConnectionError as OSConnectionError

    from app.api.endpoints import embedding_migration
    from app.tasks import embedding_migration_v4_scope as scope

    def _down(_ids):
        raise OSConnectionError("N/A", "down", Exception("down"))

    monkeypatch.setattr(scope, "_count_embeddable_speakers_per_file", _down)
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [(1,)]
    with pytest.raises(OSConnectionError):
        embedding_migration._compute_ground_truth_status(db)


def test_index_health_probe_marks_unhealthy_and_logs_traceback(monkeypatch, caplog):
    from opensearchpy.exceptions import ConnectionError as OSConnectionError

    import app.services.opensearch_service as os_service
    from app.api.endpoints import search_index_health

    def _exists(index):
        raise OSConnectionError("N/A", "down", Exception("down"))

    client = SimpleNamespace(indices=SimpleNamespace(exists=_exists))
    monkeypatch.setattr(os_service, "opensearch_client", client)
    with caplog.at_level(logging.WARNING, logger=search_index_health.__name__):
        assert search_index_health._probe_index_health(["idx"]) == {"idx": "unhealthy"}
    assert _traceback_logged(caplog, search_index_health.__name__)


def test_index_health_probe_bug_propagates(monkeypatch):
    import app.services.opensearch_service as os_service
    from app.api.endpoints import search_index_health

    def _exists(index):
        raise AttributeError("bug")

    client = SimpleNamespace(indices=SimpleNamespace(exists=_exists))
    monkeypatch.setattr(os_service, "opensearch_client", client)
    with pytest.raises(AttributeError):
        search_index_health._probe_index_health(["idx"])


def test_document_export_guard_redis_outage_logs_traceback_and_proceeds(monkeypatch, caplog):
    import app.core.redis as core_redis
    from app.api.endpoints.files import transcript_document_export as export

    def _down():
        raise RedisError("down")

    monkeypatch.setattr(core_redis, "get_request_redis", _down)
    body = MagicMock()
    monkeypatch.setattr(export, "_options_sha", lambda _b: "sha")
    with caplog.at_level(logging.WARNING, logger=export.__name__):
        _job, guard_key, is_new = export._claim_or_existing_job(1, 2, body)
    assert is_new is True
    assert guard_key is not None
    assert _traceback_logged(caplog, export.__name__)


@pytest.mark.asyncio
async def test_publish_notification_redis_outage_logs_traceback(monkeypatch, caplog):
    from app.api import websockets

    class _Down:
        async def publish(self, *_a):
            raise RedisError("down")

    monkeypatch.setattr(websockets, "redis_client", _Down())
    with caplog.at_level(logging.ERROR, logger=websockets.__name__):
        await websockets.publish_notification(1, "t", {"a": 1})
    assert _traceback_logged(caplog, websockets.__name__)


@pytest.mark.asyncio
async def test_publish_notification_bug_propagates(monkeypatch):
    from app.api import websockets

    class _Bug:
        async def publish(self, *_a):
            raise AttributeError("bug")

    monkeypatch.setattr(websockets, "redis_client", _Bug())
    with pytest.raises(AttributeError):
        await websockets.publish_notification(1, "t", {"a": 1})


def test_complete_upload_fingerprint_storage_error_logs_traceback(monkeypatch, caplog):
    from minio.error import S3Error
    from urllib3 import HTTPResponse

    import app.services.imohash_service as imohash
    from app.api.endpoints.files import complete_upload

    def _down(*_a, **_k):
        raise S3Error(HTTPResponse(status=500), "InternalError", "down", "r", "req", "host")

    monkeypatch.setattr(imohash, "compute_from_minio", _down)
    with caplog.at_level(logging.WARNING, logger=complete_upload.__name__):
        assert complete_upload._fingerprint_object(None, "p", 10) is None
    assert _traceback_logged(caplog, complete_upload.__name__)


def test_complete_upload_fingerprint_bug_propagates(monkeypatch):
    import app.services.imohash_service as imohash
    from app.api.endpoints.files import complete_upload

    def _bug(*_a, **_k):
        raise AttributeError("bug")

    monkeypatch.setattr(imohash, "compute_from_minio", _bug)
    with pytest.raises(AttributeError):
        complete_upload._fingerprint_object(None, "p", 10)


@pytest.mark.asyncio
async def test_federated_logout_http_failure_is_false_with_traceback(monkeypatch, caplog):
    import httpx

    from app.api.endpoints.auth import sessions

    monkeypatch.setattr(sessions, "_has_federated_session", lambda *_a: True)
    monkeypatch.setattr(sessions, "_federated_logout_inputs", lambda *_a: ("rt", object()))
    monkeypatch.setattr(sessions, "_clear_oidc_refresh_token", lambda *_a: None)

    async def _down(*_a, **_k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(sessions, "call_federated_logout", _down)
    with caplog.at_level(logging.WARNING, logger=sessions.__name__):
        assert await sessions._end_federated_session(MagicMock(), 1) is False
    assert _traceback_logged(caplog, sessions.__name__)


def test_local_model_state_outage_is_none_with_traceback(monkeypatch, caplog):
    from app.api.endpoints import local_models

    def _down():
        raise RedisError("down")

    monkeypatch.setattr(local_models, "_store", _down)
    with caplog.at_level(logging.WARNING, logger=local_models.__name__):
        assert local_models._current_download() is None
    assert _traceback_logged(caplog, local_models.__name__)


def test_asr_test_failure_logs_frames_but_never_the_api_key(caplog):
    from app.api.endpoints import asr_connection_tests as asr_tests

    try:
        raise RuntimeError("401 for key sk-secret-123")
    except RuntimeError as exc:
        frames = asr_tests._frames(exc)
    assert "test_asr_test_failure_logs_frames" in frames
    assert "sk-secret-123" not in frames


def test_health_probe_failure_is_in_response_and_logged_with_traceback(caplog):
    from app.api import health

    def _boom() -> str:
        raise RuntimeError("probe down")

    checks: dict[str, str] = {}
    with caplog.at_level(logging.ERROR, logger=health.__name__):
        health._run_probe(checks, "db", _boom)
    assert checks == {"db": "error: RuntimeError"}
    assert _traceback_logged(caplog, health.__name__)
