"""Unit tests for the OpenSearch snapshot leg of the scheduled backup (issue #242).

The OpenSearch client is mocked at the ``get_opensearch_client`` boundary — NO live
OpenSearch is required. Covers: repo-register idempotency, snapshot-name GFS pruning
reuse, the ``include_opensearch`` off path (no OS calls), and graceful degradation when
``path.repo`` isn't allow-listed or OpenSearch is unreachable.
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from unittest import mock

from app.services import backup_service as bs
from app.services import opensearch_snapshot as oss


def _fake_client(*, snapshots=None, repo_exists=False, ping=True):
    """Build a mock OpenSearch client whose ``.snapshot.*`` methods we can assert on."""
    snap_api = mock.MagicMock()
    snap_api.get.return_value = {"snapshots": [{"snapshot": n} for n in (snapshots or [])]}
    if repo_exists:
        snap_api.get_repository.return_value = {
            oss.REPO_NAME: {"type": "fs", "settings": {"location": oss.REPO_LOCATION}}
        }
    else:
        snap_api.get_repository.side_effect = Exception("repository_missing_exception")
    client = SimpleNamespace(snapshot=snap_api, ping=mock.MagicMock(return_value=ping))
    return client


# =============================================================================
# Repository registration idempotency
# =============================================================================
def test_ensure_repository_creates_when_absent():
    client = _fake_client(repo_exists=False)
    oss.ensure_repository(client)
    client.snapshot.create_repository.assert_called_once()
    _, kwargs = client.snapshot.create_repository.call_args
    assert kwargs["repository"] == oss.REPO_NAME
    assert kwargs["body"]["type"] == "fs"
    assert kwargs["body"]["settings"]["location"] == oss.REPO_LOCATION


def test_ensure_repository_idempotent_when_present():
    client = _fake_client(repo_exists=True)
    oss.ensure_repository(client)
    # Already registered with the right location → no re-create.
    client.snapshot.create_repository.assert_not_called()


def test_ensure_repository_recreates_on_location_mismatch():
    client = _fake_client(repo_exists=True)
    client.snapshot.get_repository.return_value = {
        oss.REPO_NAME: {"type": "fs", "settings": {"location": "/some/other/path"}}
    }
    oss.ensure_repository(client)
    client.snapshot.create_repository.assert_called_once()


# =============================================================================
# Snapshot-name GFS pruning reuses the pg-dump selector
# =============================================================================
def test_prune_snapshots_reuses_gfs_selector():
    # 5 daily snapshots; keep 2 daily → expect the 3 oldest deleted, by the SAME
    # selector backup_service uses for .dump files.
    base = datetime(2026, 6, 1, 3, 0, tzinfo=timezone.utc)
    names = [
        f"opentranscribe-{(base - timedelta(days=i)).strftime('%Y%m%d-%H%M%S')}" for i in range(5)
    ]
    client = _fake_client(snapshots=names)
    cfg = {"retention_daily": 2, "retention_weekly": 0, "retention_monthly": 0}

    deleted = oss.prune_snapshots(client, cfg)

    # The selector keeps the 2 newest; the 3 oldest are deleted. It parses the date off a
    # .dump-suffixed name, so prune_snapshots maps names → <name>.dump and back.
    selected_dump = bs.select_backups_to_delete(
        [f"{n}.dump" for n in names], retention_daily=2, retention_weekly=0, retention_monthly=0
    )
    expected = [d[: -len(".dump")] for d in selected_dump]
    assert sorted(deleted) == sorted(expected)
    assert len(deleted) == 3
    assert client.snapshot.delete.call_count == 3


def test_list_snapshot_names_filters_foreign_names():
    client = _fake_client(snapshots=["opentranscribe-20260601-030000", "something-else", "snap-1"])
    names = oss.list_snapshot_names(client)
    assert names == ["opentranscribe-20260601-030000"]


# =============================================================================
# perform_snapshot — happy path + graceful degradation
# =============================================================================
def test_perform_snapshot_ok():
    client = _fake_client(repo_exists=True, snapshots=[])
    with mock.patch.object(oss, "_client", return_value=client):
        res = oss.perform_snapshot(
            {"retention_daily": 7, "retention_weekly": 4, "retention_monthly": 12},
            ts="20260607-030000",
        )
    assert res["status"] == "ok"
    assert res["snapshot"] == "opentranscribe-20260607-030000"
    assert res["repository"] == oss.REPO_NAME
    client.snapshot.create.assert_called_once()
    _, kwargs = client.snapshot.create.call_args
    assert kwargs["wait_for_completion"] is True


def test_perform_snapshot_skipped_when_unreachable():
    client = _fake_client(ping=False)
    with mock.patch.object(oss, "_client", return_value=client):
        res = oss.perform_snapshot(
            {"retention_daily": 7, "retention_weekly": 4, "retention_monthly": 12}
        )
    assert res["status"] == "skipped"
    client.snapshot.create.assert_not_called()


def test_perform_snapshot_unsupported_when_repo_register_fails():
    # path.repo not allow-listed → create_repository raises repository_exception.
    client = _fake_client(repo_exists=False)
    client.snapshot.create_repository.side_effect = Exception(
        "repository_exception: [opentranscribe_backup] location [...] doesn't match path.repo"
    )
    with mock.patch.object(oss, "_client", return_value=client):
        res = oss.perform_snapshot(
            {"retention_daily": 7, "retention_weekly": 4, "retention_monthly": 12}
        )
    assert res["status"] == "unsupported"
    assert "path.repo" in res["error"]
    client.snapshot.create.assert_not_called()


def test_perform_snapshot_error_when_create_fails():
    client = _fake_client(repo_exists=True)
    client.snapshot.create.side_effect = Exception("snapshot create boom")
    with mock.patch.object(oss, "_client", return_value=client):
        res = oss.perform_snapshot(
            {"retention_daily": 7, "retention_weekly": 4, "retention_monthly": 12}
        )
    assert res["status"] == "error"
    assert "boom" in res["error"]


def test_snapshot_status_reachable_with_repo_and_last():
    client = _fake_client(repo_exists=True, snapshots=["opentranscribe-20260601-030000"])
    with mock.patch.object(oss, "_client", return_value=client):
        st = oss.snapshot_status()
    assert st["reachable"] is True
    assert st["repository_registered"] is True
    assert st["last_snapshot"] == "opentranscribe-20260601-030000"


def test_snapshot_status_unreachable():
    client = _fake_client(ping=False)
    with mock.patch.object(oss, "_client", return_value=client):
        st = oss.snapshot_status()
    assert st == {"reachable": False, "repository_registered": False, "last_snapshot": None}


# =============================================================================
# perform_backup integration — toggle off = no OS calls; toggle on = sub-result
# =============================================================================
def _fake_pg_dump(cmd, **kwargs):
    out = kwargs.get("stdout")
    if out is not None:
        out.write(b"FAKE PGDUMP DATA")
        out.flush()
    return subprocess.CompletedProcess(cmd, 0, b"", b"")


def test_perform_backup_no_opensearch_calls_when_disabled(db_session, tmp_path):
    bs.update_settings(db_session, destination=str(tmp_path), include_opensearch=False)
    with (
        mock.patch("app.services.backup_service.subprocess.run", side_effect=_fake_pg_dump),
        mock.patch("app.services.opensearch_snapshot.perform_snapshot") as snap,
    ):
        result = bs.perform_backup(db_session)
    assert result["ok"] is True
    assert "opensearch" not in result
    snap.assert_not_called()


def test_perform_backup_records_opensearch_subresult_when_enabled(db_session, tmp_path):
    bs.update_settings(db_session, destination=str(tmp_path), include_opensearch=True)
    with (
        mock.patch("app.services.backup_service.subprocess.run", side_effect=_fake_pg_dump),
        mock.patch(
            "app.services.opensearch_snapshot.perform_snapshot",
            return_value={"status": "ok", "snapshot": "opentranscribe-20260607-030000"},
        ) as snap,
    ):
        result = bs.perform_backup(db_session)
    assert result["ok"] is True
    assert result["opensearch"]["status"] == "ok"
    # pg success is independent of OS — the timestamp stem is passed through.
    snap.assert_called_once()
    assert snap.call_args.kwargs["ts"] == result["filename"][len("opentranscribe-") : -len(".dump")]


def test_perform_backup_pg_ok_even_when_opensearch_errors(db_session, tmp_path):
    bs.update_settings(db_session, destination=str(tmp_path), include_opensearch=True)
    with (
        mock.patch("app.services.backup_service.subprocess.run", side_effect=_fake_pg_dump),
        mock.patch(
            "app.services.opensearch_snapshot.perform_snapshot",
            return_value={"status": "unsupported", "error": "path.repo not allow-listed"},
        ),
    ):
        result = bs.perform_backup(db_session)
    # pg backup still ok; the OS failure is recorded but never flips overall success.
    assert result["ok"] is True
    assert result["status"] == "success"
    assert result["opensearch"]["status"] == "unsupported"
