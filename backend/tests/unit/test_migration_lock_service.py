"""Tests for the migration lock service (ref-counted implementation).

Note: The migration lock is no longer used in the application — Celery
priorities handle task ordering. These tests verify the ref-counted
implementation in case it's needed in the future.
"""

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest


class TestMigrationLockService:
    """Unit tests for MigrationLockService (ref-counted)."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        """Create a fresh service instance with mocked Redis for each test."""
        from app.services.migration_lock_service import MigrationLockService

        self.service = MigrationLockService()
        self.mock_redis = MagicMock()
        self.mock_redis.ping.return_value = True
        self.service._client = self.mock_redis

    # ---- activate (INCR-based) ----

    def test_activate_acquires_lock(self):
        self.mock_redis.incr.return_value = 1
        assert self.service.activate() is True
        self.mock_redis.incr.assert_called_once()
        self.mock_redis.expire.assert_called_once()

    def test_activate_increments_ref_count(self):
        self.mock_redis.incr.return_value = 2  # Second activation
        assert self.service.activate() is True
        self.mock_redis.incr.assert_called_once()

    def test_activate_returns_false_when_redis_unavailable(self):
        self.service._client = None
        with patch("app.services.migration_lock_service.get_request_redis") as mock_fn:
            mock_fn.side_effect = ConnectionError("down")
            assert self.service.activate() is False

    # ---- deactivate (DECR-based) ----

    def test_deactivate_releases_lock_at_zero(self):
        self.mock_redis.decr.return_value = 0
        assert self.service.deactivate() is True
        self.mock_redis.delete.assert_called_once()

    def test_deactivate_decrements_but_keeps_lock(self):
        self.mock_redis.decr.return_value = 1  # Still held by another migration
        assert self.service.deactivate() is False
        self.mock_redis.delete.assert_not_called()

    def test_deactivate_cleans_up_negative_count(self):
        self.mock_redis.decr.return_value = -1  # Edge case
        assert self.service.deactivate() is True
        self.mock_redis.delete.assert_called_once()

    # ---- is_active ----

    def test_is_active_true_when_count_positive(self):
        self.mock_redis.get.return_value = "2"
        assert self.service.is_active() is True

    def test_is_active_false_when_key_missing(self):
        self.mock_redis.get.return_value = None
        assert self.service.is_active() is False

    def test_is_active_false_when_count_zero(self):
        self.mock_redis.get.return_value = "0"
        assert self.service.is_active() is False

    def test_is_active_false_when_redis_unavailable(self):
        self.service._client = None
        with patch("app.services.migration_lock_service.get_request_redis") as mock_fn:
            mock_fn.side_effect = ConnectionError("down")
            assert self.service.is_active() is False

    # ---- lifecycle ----

    def test_activate_then_deactivate(self):
        self.mock_redis.incr.return_value = 1
        self.mock_redis.get.return_value = "1"
        self.mock_redis.decr.return_value = 0

        assert self.service.activate() is True
        assert self.service.is_active() is True
        assert self.service.deactivate() is True
