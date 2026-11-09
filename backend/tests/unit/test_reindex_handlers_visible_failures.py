"""Reindex helpers: an OpenSearch outage is visible (traceback, distinct value), a bug is not hidden."""

import logging
from unittest.mock import MagicMock

import pytest
from opensearchpy.exceptions import ConnectionError as OpenSearchConnectionError
from redis.exceptions import ConnectionError as RedisConnectionError

from app.tasks import reindex_completion
from app.tasks import reindex_index_ops
from app.tasks import reindex_run_state


def _traceback_logged(caplog, fragment: str) -> bool:
    return any(fragment in r.getMessage() and r.exc_info for r in caplog.records)


def _down() -> OpenSearchConnectionError:
    return OpenSearchConnectionError("N/A", "connection refused", Exception("refused"))


@pytest.fixture
def os_client(monkeypatch):
    from app.services import opensearch_service

    client = MagicMock()
    monkeypatch.setattr(opensearch_service, "opensearch_client", client)
    return client


def test_failure_to_restore_refresh_interval_is_an_error_with_a_traceback(os_client, caplog):
    os_client.indices.put_settings.side_effect = _down()
    with caplog.at_level(logging.WARNING, logger=reindex_index_ops.logger.name):
        reindex_index_ops._restore_normal_mode()
    errors = [r for r in caplog.records if "Could not restore normal mode" in r.getMessage()]
    assert errors
    assert errors[0].levelno == logging.ERROR
    assert errors[0].exc_info


def test_refresh_outage_is_traced_and_a_bug_propagates(os_client, caplog):
    os_client.indices.refresh.side_effect = _down()
    with caplog.at_level(logging.WARNING, logger=reindex_index_ops.logger.name):
        reindex_index_ops._refresh_index_and_clear_cache()
    assert _traceback_logged(caplog, "Index refresh after reindex failed")

    os_client.indices.refresh.side_effect = KeyError("bug")
    with pytest.raises(KeyError):
        reindex_index_ops._refresh_index_and_clear_cache()


def test_orphan_cleanup_that_cannot_look_returns_none_not_zero(os_client, caplog):
    os_client.indices.exists.side_effect = _down()
    with caplog.at_level(logging.ERROR, logger=reindex_completion.logger.name):
        result = reindex_completion._cleanup_orphaned_chunks(1, {"u-1"})
    assert result is None
    assert _traceback_logged(caplog, "Post-reindex orphan cleanup failed")


def test_orphan_cleanup_with_nothing_to_remove_still_returns_zero(os_client):
    os_client.indices.exists.return_value = False
    assert reindex_completion._cleanup_orphaned_chunks(1, {"u-1"}) == 0


def test_stale_progress_clear_failure_is_traced(monkeypatch, caplog):
    redis = MagicMock()
    redis.delete.side_effect = RedisConnectionError("redis down")
    monkeypatch.setattr(reindex_run_state, "get_redis", lambda: redis)
    with caplog.at_level(logging.WARNING, logger=reindex_run_state.logger.name):
        reindex_run_state._clear_stale_progress(1)
    assert _traceback_logged(caplog, "Could not clear stale reindex progress")
