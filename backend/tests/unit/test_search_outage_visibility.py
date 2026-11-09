"""Outage visibility for ``services/search`` handlers (broad-except lane B9).

Every case drives one handler with a dependency that is down and asserts the failure is
visible (return value that differs from the healthy-empty one, or a raise) and that the
log record carries ``exc_info``. The companion assertion is that a programming error is
not swallowed into the same outcome.
"""

import logging
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from opensearchpy.exceptions import ConnectionError as OpenSearchConnectionError
from opensearchpy.exceptions import NotFoundError
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError

from app.core.exceptions import ServiceUnavailableError
from app.services.search import ml_model_service
from app.services.search import neural_bootstrap
from app.services.search import pipelines
from app.services.search import reindex_cancel
from app.services.search import search_infra
from app.services.search import settings_service


def _os_down() -> OpenSearchConnectionError:
    return OpenSearchConnectionError("N/A", "cluster down", Exception())


def _tracebacks(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.exc_info]


def _ml_service(exc: Exception) -> Any:
    client = MagicMock()
    client.transport.perform_request.side_effect = exc
    with patch.object(ml_model_service, "get_opensearch_client", return_value=client):
        return ml_model_service.OpenSearchMLModelService()


class TestModelRegistry:
    def test_status_raises_when_asked_and_registry_down(self, caplog):
        svc = _ml_service(_os_down())
        with caplog.at_level(logging.DEBUG), pytest.raises(ServiceUnavailableError):
            svc.get_model_status("m", raise_on_error=True)
        assert _tracebacks(caplog)

    def test_status_default_stays_soft_but_logs_traceback(self, caplog):
        svc = _ml_service(_os_down())
        with caplog.at_level(logging.DEBUG):
            assert svc.get_model_status("m") == {}
        assert _tracebacks(caplog)

    def test_unknown_model_is_empty_not_an_outage(self, caplog):
        svc = _ml_service(NotFoundError(404, "not_found", {}))
        with caplog.at_level(logging.DEBUG):
            assert svc.get_model_status("m", raise_on_error=True) == {}
        assert not _tracebacks(caplog)

    def test_configure_settings_reports_outage_with_traceback(self, caplog):
        client = MagicMock()
        client.cluster.put_settings.side_effect = _os_down()
        with (
            patch.object(ml_model_service, "get_opensearch_client", return_value=client),
            caplog.at_level(logging.DEBUG),
        ):
            assert ml_model_service.OpenSearchMLModelService().configure_ml_settings() is False
        assert _tracebacks(caplog)

    def test_deploy_reports_outage_with_traceback_and_not_a_bug(self, caplog):
        svc = _ml_service(_os_down())
        with caplog.at_level(logging.DEBUG):
            assert svc.deploy_model("m") is False
        assert _tracebacks(caplog)

        with pytest.raises(RuntimeError, match="bug"):
            _ml_service(RuntimeError("bug")).deploy_model("m")


class TestSettingsWrite:
    def test_failed_save_is_raised_not_swallowed(self, caplog):
        boom = OperationalError("INSERT", {}, Exception("db down"))
        with (
            patch("app.db.session_utils.session_scope", side_effect=boom),
            caplog.at_level(logging.DEBUG),
            pytest.raises(OperationalError),
        ):
            settings_service._set_setting("k", "v")
        assert _tracebacks(caplog)


class TestReindexCancel:
    def test_unreachable_redis_reads_as_no_cancellation_with_traceback(self, caplog):
        with (
            patch("app.core.redis.get_request_redis", side_effect=RedisConnectionError("down")),
            caplog.at_level(logging.DEBUG),
        ):
            assert reindex_cancel.cancel_target(1) is None
        assert _tracebacks(caplog)

    def test_a_programming_error_is_not_read_as_no_cancellation(self):
        with (
            patch("app.core.redis.get_request_redis", side_effect=RuntimeError("bug")),
            pytest.raises(RuntimeError, match="bug"),
        ):
            reindex_cancel.cancel_target(1)

    @pytest.mark.parametrize("fn", [reindex_cancel.clear_cancel, reindex_cancel.clear_fanout])
    def test_clear_logs_redis_outage_with_traceback(self, fn: Callable[[int], None], caplog):
        with (
            patch("app.core.redis.get_request_redis", side_effect=RedisConnectionError("down")),
            caplog.at_level(logging.DEBUG),
        ):
            fn(1)
        assert _tracebacks(caplog)


class TestNeuralBootstrapState:
    def test_increment_logs_redis_outage_with_traceback(self, caplog):
        redis_client = MagicMock()
        redis_client.incr.side_effect = RedisConnectionError("down")
        with caplog.at_level(logging.DEBUG):
            assert neural_bootstrap._increment_attempts(redis_client) == 1
        assert _tracebacks(caplog)

    def test_record_failure_logs_redis_outage_with_traceback(self, caplog):
        redis_client = MagicMock()
        redis_client.set.side_effect = RedisConnectionError("down")
        with caplog.at_level(logging.DEBUG):
            neural_bootstrap._record_failure(redis_client, "err", 1.0)
        assert _tracebacks(caplog)

    def test_increment_does_not_swallow_a_programming_error(self):
        redis_client = MagicMock()
        redis_client.incr.side_effect = RuntimeError("bug")
        with pytest.raises(RuntimeError, match="bug"):
            neural_bootstrap._increment_attempts(redis_client)


class TestInfrastructure:
    def test_index_probe_outage_reads_as_absent_with_traceback(self, caplog):
        client = MagicMock()
        client.indices.exists.side_effect = _os_down()
        with (
            patch.object(search_infra, "_index_verified", False),
            caplog.at_level(logging.DEBUG),
        ):
            assert search_infra._chunks_index_verified(client, "idx") is False
        assert _tracebacks(caplog)

    def test_pipeline_creation_outage_is_reported_with_traceback(self, caplog):
        client = MagicMock()
        client.transport.perform_request.side_effect = _os_down()
        with (
            patch("app.services.opensearch_service.client.opensearch_client", client),
            caplog.at_level(logging.DEBUG),
        ):
            assert pipelines.ensure_search_pipeline_exists() is False
        assert _tracebacks(caplog)
