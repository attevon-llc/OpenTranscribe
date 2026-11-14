"""Progress updates must cost O(1) Redis work regardless of failures so far (loadrun2 R1)."""

from unittest.mock import MagicMock

import pytest

from app.services import migration_progress_service as mps
from app.services.migration_progress_service import MigrationProgressService

pytestmark = pytest.mark.unit


def _svc(client):
    svc = MigrationProgressService(key_prefix="t")
    svc._redis_client = client
    return svc


def test_increment_script_never_decodes_or_encodes_the_status_blob():
    lua = MigrationProgressService._INCREMENT_LUA
    assert "cjson" not in lua
    assert "ipairs" not in lua  # no linear scan of a growing list


def test_increment_is_one_eval_with_payload_independent_of_failure_count():
    client = MagicMock()
    svc = _svc(client)
    sizes = []
    for i in range(500):
        client.eval.reset_mock()
        assert svc.increment_processed(success=False, file_uuid=f"uuid-{i}")
        assert client.eval.call_count == 1
        client.get.assert_not_called()
        client.set.assert_not_called()
        sizes.append(sum(len(str(a)) for a in client.eval.call_args.args[2:]))
    assert max(sizes) - min(sizes) <= 4  # only the uuid text differs
    assert client.eval.call_args.args[-1] == mps.MAX_RECORDED_FAILED_FILES


def test_get_status_overlays_counters_and_failed_set_with_same_shape():
    client = MagicMock()
    client.ping.return_value = True
    client.get.return_value = (
        b'{"running": true, "total_files": 9, "processed_files": 0, '
        b'"failed_files": [], "started_at": null, "completed_at": null, '
        b'"orchestrator_task_id": null, "last_updated": null}'
    )
    client.hgetall.return_value = {b"processed_files": b"3", b"last_updated": b"now"}
    client.smembers.return_value = {b"b", b"a"}
    status = _svc(client).get_status()
    assert status["processed_files"] == 3
    assert status["failed_files"] == ["a", "b"]
    assert status["total_files"] == 9
