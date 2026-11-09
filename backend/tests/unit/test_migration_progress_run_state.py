"""A Redis outage must not read as "no migration running" (fail closed)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from redis.exceptions import ConnectionError as RedisConnectionError

from app.services.migration_progress_service import MigrationProgressService
from app.services.migration_progress_service import RunState


def _svc(client: MagicMock) -> MigrationProgressService:
    svc = MigrationProgressService(key_prefix="run_state_test")
    svc._redis_client = client
    return svc


def test_unreadable_status_is_unknown_and_counts_as_running():
    client = MagicMock()
    client.get.side_effect = RedisConnectionError("dropped mid-read")
    svc = _svc(client)
    assert svc.running_state() is RunState.UNKNOWN
    assert svc.is_running() is True


def test_a_missing_status_is_idle():
    client = MagicMock()
    client.get.return_value = None
    svc = _svc(client)
    assert svc.running_state() is RunState.IDLE
    assert svc.is_running() is False


def test_a_running_status_is_running_and_a_finished_one_is_idle():
    client = MagicMock()
    svc = _svc(client)
    client.get.return_value = json.dumps({"running": True})
    assert svc.running_state() is RunState.RUNNING
    client.get.return_value = json.dumps({"running": False})
    assert svc.running_state() is RunState.IDLE


def test_a_corrupt_status_is_unknown_not_idle():
    client = MagicMock()
    client.get.return_value = "{not json"
    assert _svc(client).running_state() is RunState.UNKNOWN
