"""The storage orphan sweep routes (#1212): authz tiers, the confirm gate, the audit trail.

The Celery task and its Redis lock are replaced with recorders, so no object is ever
listed or deleted here (the sweep itself is proven against a real MinIO in
``tests/integration/test_orphan_sweep_storage.py``). What is asserted is the contract the
admin panel depends on: nothing dispatches without ``confirm=true``, ``force`` reaches the
task, the request is on the audit trail, and a failing scan answers with a fixed code and
none of the exception's text.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import status

BASE = "/api/admin/data-integrity/storage"
MODULE = "app.tasks.storage_orphan_sweep_task"

_REPORT = {
    "dry_run": True,
    "force": False,
    "refused": None,
    "error": None,
    "total_objects": 10,
    "total_bytes": 100,
    "orphan_files": 1,
    "orphan_objects": 5,
    "orphan_bytes": 50,
    "orphans": [{"file_id": 9, "user_id": 2, "objects": 5, "bytes": 50}],
    "orphans_truncated": False,
    "skipped_recent_files": 0,
    "rechecked_present": 0,
    "deleted_files": 0,
    "deleted_objects": 0,
    "deleted_bytes": 0,
    "failed_objects": 0,
}


class _RecordingSweep:
    def __init__(self) -> None:
        self.dispatches: list[dict] = []

    def delay(self, **kwargs) -> SimpleNamespace:
        self.dispatches.append(kwargs)
        return SimpleNamespace(id="stand-in-storage-sweep-id")


@pytest.fixture
def seams():
    sweep = _RecordingSweep()
    state = {"running": False, "last_run": None}
    with (
        patch(f"{MODULE}.storage_orphan_sweep_task", sweep),
        patch(f"{MODULE}.get_storage_sweep_status", lambda: dict(state)),
        patch(f"{MODULE}.get_storage_orphan_counts", lambda: dict(_REPORT)),
    ):
        yield SimpleNamespace(sweep=sweep, state=state)


def test_counts_returns_the_dry_run_report(client, admin_token_headers, seams):
    response = client.get(f"{BASE}/counts", headers=admin_token_headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["orphan_files"] == 1
    assert body["orphan_bytes"] == 50
    assert body["orphans"] == [{"file_id": 9, "user_id": 2, "objects": 5, "bytes": 50}]
    assert seams.sweep.dispatches == []


def test_a_sweep_without_confirm_is_refused_and_dispatches_nothing(
    client, admin_token_headers, seams
):
    for query in ("", "?force=true"):
        response = client.post(f"{BASE}{query}", headers=admin_token_headers)
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "confirm=true" in response.json()["detail"]
    assert seams.sweep.dispatches == []


def test_a_confirmed_sweep_dispatches_for_the_requesting_admin_and_is_audited(
    client, admin_token_headers, admin_user, seams, caplog
):
    with caplog.at_level("INFO", logger="audit"):
        response = client.post(f"{BASE}?confirm=true", headers=admin_token_headers)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "started"
    assert seams.sweep.dispatches == [{"user_id": admin_user.id, "force": False}]
    events = [
        r.getMessage()
        for r in caplog.records
        if "admin.data_integrity.storage_purge" in r.getMessage()
    ]
    assert len(events) == 1
    assert f'"user_id": {admin_user.id}' in events[0]


def test_force_reaches_the_task(client, admin_token_headers, admin_user, seams):
    response = client.post(f"{BASE}?confirm=true&force=true", headers=admin_token_headers)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["force"] is True
    assert seams.sweep.dispatches == [{"user_id": admin_user.id, "force": True}]


def test_a_sweep_already_running_dispatches_nothing(client, admin_token_headers, seams):
    seams.state["running"] = True

    response = client.post(f"{BASE}?confirm=true", headers=admin_token_headers)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "already_running"
    assert seams.sweep.dispatches == []


_GUARDED = [
    ("get", f"{BASE}/counts"),
    ("get", f"{BASE}/status"),
    ("post", f"{BASE}?confirm=true"),
]


@pytest.mark.parametrize(("method", "path"), _GUARDED)
def test_a_plain_user_is_refused(client, user_token_headers, seams, method, path):
    response = getattr(client, method)(path, headers=user_token_headers)

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert seams.sweep.dispatches == []


@pytest.mark.parametrize(("method", "path"), _GUARDED)
def test_anonymous_is_unauthenticated(client, seams, method, path):
    assert getattr(client, method)(path).status_code == status.HTTP_401_UNAUTHORIZED
    assert seams.sweep.dispatches == []


def test_status_reports_running_and_last_run(client, admin_token_headers, seams):
    seams.state["running"] = True
    seams.state["last_run"] = {
        "timestamp": 1.0,
        "duration_seconds": 2.0,
        "results": {**_REPORT, "dry_run": False, "deleted_files": 1},
    }

    body = client.get(f"{BASE}/status", headers=admin_token_headers).json()

    assert body["running"] is True
    assert body["last_run"]["results"]["deleted_files"] == 1


def test_a_failed_listing_returns_a_fixed_code_not_the_exception_text(monkeypatch):
    """The sweep reports ``storage_listing_failed``; the cause stays in the server log."""
    from app.services import minio_service
    from app.tasks import storage_orphan_sweep_task as sweeper

    class _Boom:
        def list_objects(self, *args, **kwargs):
            raise RuntimeError("SECRET-endpoint-detail http://internal:9000")

    monkeypatch.setattr(sweeper, "_get_all_file_ids_from_db", lambda: {1})
    monkeypatch.setattr(minio_service, "minio_client", _Boom())

    result = sweeper.run_storage_orphan_sweep(dry_run=True)

    assert result["error"] == "storage_listing_failed"
    assert "SECRET" not in repr(result)
