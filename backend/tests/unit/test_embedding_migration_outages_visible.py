"""Outages in the v3->v4 embedding migration helpers must be loud, not a plausible default."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from opensearchpy.exceptions import ConnectionError as OSConnectionError
from redis.exceptions import ConnectionError as RedisConnectionError

from app.tasks import embedding_migration_v4_finalize as finalize
from app.tasks import embedding_migration_v4_scope as scope
from app.tasks import embedding_migration_v4_writer as writer


def _os_down() -> OSConnectionError:
    return OSConnectionError("N/A", "down", Exception("down"))


def test_clear_v4_index_failure_propagates_instead_of_continuing_over_stale_docs(caplog):
    client = MagicMock()
    client.indices.exists.return_value = True
    client.count.return_value = {"count": 3}
    client.delete_by_query.side_effect = _os_down()
    with (
        patch("app.services.opensearch_service.get_opensearch_client", return_value=client),
        caplog.at_level(logging.ERROR),
        pytest.raises(OSConnectionError),
    ):
        scope._clear_v4_index()
    assert any(r.exc_info for r in caplog.records)


def test_skipped_by_policy_count_is_unknown_not_zero_when_redis_is_down(caplog):
    redis = MagicMock()
    redis.get.side_effect = RedisConnectionError("down")
    with (
        patch("app.core.redis.get_redis", return_value=redis),
        caplog.at_level(logging.WARNING),
    ):
        assert scope._take_skipped_by_policy() is None
    assert any(r.exc_info for r in caplog.records)


def test_skipped_by_policy_count_reads_real_zero_and_real_values():
    redis = MagicMock()
    redis.get.return_value = None
    with patch("app.core.redis.get_redis", return_value=redis):
        assert scope._take_skipped_by_policy() == 0
    redis.get.return_value = b"4"
    with patch("app.core.redis.get_redis", return_value=redis):
        assert scope._take_skipped_by_policy() == 4


def test_bulk_write_failure_logs_traceback_and_reports_zero_written(caplog):
    client = MagicMock()
    client.bulk.side_effect = _os_down()
    with (
        patch("app.services.opensearch_service.get_opensearch_client", return_value=client),
        patch(
            "app.services.opensearch_service.speaker_write.availability_by_file",
            return_value={},
        ),
        patch("app.services.opensearch_service.speaker_write.stamp_availability"),
        caplog.at_level(logging.ERROR),
    ):
        written = writer._bulk_write_v4_embeddings([{"_id": "a", "media_file_id": 1}])
    assert written == 0
    assert any(r.exc_info for r in caplog.records)


def test_profile_count_is_none_not_zero_when_the_count_query_fails(caplog):
    client = MagicMock()
    client.indices.exists.return_value = True
    client.count.side_effect = _os_down()
    with caplog.at_level(logging.ERROR):
        assert finalize._count_profile_docs(client, "idx") is None
    assert any(r.exc_info for r in caplog.records)


def test_profile_backfill_scroll_failure_logs_traceback_and_writes_nothing(caplog):
    client = MagicMock()
    client.search.side_effect = _os_down()
    with caplog.at_level(logging.ERROR):
        assert finalize.migrate_profile_documents_to_v4(client, "idx") == 0
    assert any(r.exc_info for r in caplog.records)
