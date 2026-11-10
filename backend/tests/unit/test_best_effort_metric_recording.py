"""A broken metrics backend must not break the cache it instruments."""

from __future__ import annotations

import logging

import pytest

from app.core import metrics as metrics_module
from app.services import redis_cache_service


@pytest.mark.parametrize("error", [ValueError("bad label"), ImportError("no prometheus")])
def test_cache_metric_failure_is_logged_not_raised(monkeypatch, caplog, error):
    class _Broken:
        def labels(self, **_kw):
            raise error

    monkeypatch.setattr(metrics_module, "cache_operations_total", _Broken())

    with caplog.at_level(logging.DEBUG, logger=redis_cache_service.logger.name):
        redis_cache_service._record("hit")  # must not raise

    assert "cache metric not recorded" in caplog.text
