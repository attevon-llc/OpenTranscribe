"""Unit tests for the scheduled-backup service (Feature C).

Covers: settings round-trip (DB-backed, savepoint-rolled-back), cron parsing + due-check
with frozen times, GFS retention pruning over fake dump files, and the destination-missing
no-op path. The ``pg_dump``/``gpg`` subprocess boundary is mocked — no real DB dump runs.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from datetime import timezone
from pathlib import Path
from unittest import mock

import pytest

from app.core import constants as C  # noqa: N812
from app.services import backup_service as bs
from app.services import system_settings_service as sss


# =============================================================================
# Settings round-trip (DB-backed)
# =============================================================================
def test_get_settings_returns_coded_defaults(db_session):
    # Precondition: no backup.* rows persisted. The dev stack may carry admin
    # overrides from earlier manual testing, so clear them within this savepoint
    # (rolled back at teardown) to assert the unset/coded-default behavior
    # deterministically — never depends on ambient DB state.
    from app.models.system_settings import SystemSettings

    db_session.query(SystemSettings).filter(SystemSettings.key.like("backup.%")).delete(
        synchronize_session=False
    )

    cfg = bs.get_settings(db_session)
    assert cfg["enabled"] is C.DEFAULT_BACKUP_ENABLED
    assert cfg["schedule"] == C.DEFAULT_BACKUP_SCHEDULE
    assert cfg["destination"] == C.DEFAULT_BACKUP_DESTINATION
    assert cfg["retention_daily"] == C.DEFAULT_BACKUP_RETENTION_DAILY
    assert cfg["retention_weekly"] == C.DEFAULT_BACKUP_RETENTION_WEEKLY
    assert cfg["retention_monthly"] == C.DEFAULT_BACKUP_RETENTION_MONTHLY
    assert cfg["encrypt"] is C.DEFAULT_BACKUP_ENCRYPT
    assert cfg["include_opensearch"] is C.DEFAULT_BACKUP_INCLUDE_OPENSEARCH
    assert cfg["last_run_at"] is None
    assert cfg["last_result"] is None


def test_update_settings_roundtrip(db_session):
    out = bs.update_settings(
        db_session,
        enabled=True,
        schedule="30 2 * * *",
        destination="/mnt/backups",
        retention_daily=3,
        retention_weekly=2,
        retention_monthly=1,
        encrypt=True,
        passphrase_file="/backups/.pass",
        include_opensearch=True,
    )
    assert out["enabled"] is True
    assert out["schedule"] == "30 2 * * *"
    assert out["destination"] == "/mnt/backups"
    assert out["retention_daily"] == 3
    assert out["encrypt"] is True
    assert out["passphrase_file"] == "/backups/.pass"
    # Re-read from DB (fresh fetch) confirms persistence.
    assert bs.get_settings(db_session)["schedule"] == "30 2 * * *"


def test_update_settings_rejects_bad_cron(db_session):
    with pytest.raises(ValueError, match="Invalid cron"):
        bs.update_settings(db_session, schedule="not a cron")


def test_partial_update_leaves_other_fields(db_session):
    bs.update_settings(db_session, enabled=True, retention_daily=9)
    bs.update_settings(db_session, retention_daily=4)
    cfg = bs.get_settings(db_session)
    assert cfg["enabled"] is True  # untouched
    assert cfg["retention_daily"] == 4


# =============================================================================
# Cron parsing
# =============================================================================
@pytest.mark.parametrize(
    "expr",
    ["0 3 * * *", "*/5 * * * *", "30 2 * * 1", "0 0 1 * *", "15 1,13 * * *", "0 9-17 * * 1-5"],
)
def test_valid_cron(expr):
    assert bs.is_valid_cron(expr)


@pytest.mark.parametrize(
    "expr",
    ["", "0 3 * *", "60 3 * * *", "0 24 * * *", "0 3 * * 9", "a b c d e", "0 3 * * */0"],
)
def test_invalid_cron(expr):
    assert not bs.is_valid_cron(expr)


# =============================================================================
# Due-check (frozen times)
# =============================================================================
def _utc(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def test_first_run_only_fires_on_matching_minute():
    # last_run_at=None → only the current minute is checked.
    assert bs.is_due("0 3 * * *", None, _utc(2026, 6, 7, 3, 0)) is True
    assert bs.is_due("0 3 * * *", None, _utc(2026, 6, 7, 3, 1)) is False


def test_due_when_matching_minute_in_gap():
    # Beat last fired at 02:55; now 03:05 → the 03:00 cron minute is in the window.
    last = _utc(2026, 6, 7, 2, 55).isoformat()
    assert bs.is_due("0 3 * * *", last, _utc(2026, 6, 7, 3, 5)) is True


def test_not_due_when_no_match_in_gap():
    last = _utc(2026, 6, 7, 3, 5).isoformat()
    assert bs.is_due("0 3 * * *", last, _utc(2026, 6, 7, 3, 50)) is False


def test_not_due_when_last_run_after_now():
    last = _utc(2026, 6, 7, 4, 0).isoformat()
    assert bs.is_due("0 3 * * *", last, _utc(2026, 6, 7, 3, 30)) is False


def test_does_not_refire_same_window():
    # Already ran at exactly 03:00; a tick at 03:02 must not re-fire.
    last = _utc(2026, 6, 7, 3, 0).isoformat()
    assert bs.is_due("0 3 * * *", last, _utc(2026, 6, 7, 3, 2)) is False


def test_stale_last_run_still_fires_once():
    # Weeks of downtime; should still detect a due minute (capped scan).
    last = _utc(2026, 5, 1, 3, 0).isoformat()
    assert bs.is_due("0 3 * * *", last, _utc(2026, 6, 7, 3, 0)) is True


def test_weekly_cron_day_of_week():
    # "30 2 * * 1" = Mondays 02:30. 2026-06-08 is a Monday.
    assert bs.is_due("30 2 * * 1", None, _utc(2026, 6, 8, 2, 30)) is True
    assert bs.is_due("30 2 * * 1", None, _utc(2026, 6, 9, 2, 30)) is False  # Tuesday


# =============================================================================
# GFS retention pruning
# =============================================================================
def _name(dt: datetime, gpg: bool = False) -> str:
    suffix = ".dump.gpg" if gpg else ".dump"
    return f"opentranscribe-{dt:%Y%m%d-%H%M%S}{suffix}"


def test_select_keeps_recent_dailies():
    names = [_name(_utc(2026, 6, d, 3, 0)) for d in range(1, 11)]  # 10 consecutive days
    to_delete = bs.select_backups_to_delete(
        names, retention_daily=7, retention_weekly=0, retention_monthly=0
    )
    # Keeps the 7 newest (June 4-10), deletes the 3 oldest (June 1-3).
    assert len(to_delete) == 3
    assert _name(_utc(2026, 6, 1, 3, 0)) in to_delete
    assert _name(_utc(2026, 6, 10, 3, 0)) not in to_delete


def test_select_ignores_unknown_files():
    names = ["random.txt", "notes.md", _name(_utc(2026, 6, 1, 3, 0))]
    to_delete = bs.select_backups_to_delete(
        names, retention_daily=0, retention_weekly=0, retention_monthly=0
    )
    assert "random.txt" not in to_delete
    assert "notes.md" not in to_delete


def test_gfs_tiers_combine():
    # Daily backups for 90 days; daily=7, weekly=4, monthly=3 should keep a spread.
    base = _utc(2026, 6, 30, 3, 0)
    names = [
        _name(base.replace(day=1) - __import__("datetime").timedelta(days=i)) for i in range(90)
    ]
    to_delete = bs.select_backups_to_delete(
        names, retention_daily=7, retention_weekly=4, retention_monthly=3
    )
    kept = set(names) - set(to_delete)
    # At least daily(7) + some weekly/monthly survive; far fewer than 90.
    assert 7 <= len(kept) <= 20
    assert len(to_delete) == 90 - len(kept)


def test_prune_backups_deletes_on_disk(tmp_path):
    for d in range(1, 11):
        (tmp_path / _name(_utc(2026, 6, d, 3, 0))).write_text("dump")
    cfg = {"retention_daily": 7, "retention_weekly": 0, "retention_monthly": 0}
    deleted = bs.prune_backups(str(tmp_path), cfg)
    assert len(deleted) == 3
    remaining = {p.name for p in tmp_path.iterdir()}
    assert len(remaining) == 7


def test_prune_backups_no_dir_returns_empty():
    assert (
        bs.prune_backups(
            "/nonexistent/path/zzz",
            {"retention_daily": 7, "retention_weekly": 0, "retention_monthly": 0},
        )
        == []
    )


# =============================================================================
# Destination status + listing
# =============================================================================
def test_destination_status_missing():
    st = bs.destination_status("/nonexistent/zzz")
    assert st["exists"] is False
    assert st["writable"] is False
    assert st["mounted"] is False


def test_destination_status_writable(tmp_path):
    st = bs.destination_status(str(tmp_path))
    assert st["exists"] is True
    assert st["writable"] is True


def test_list_backups_newest_first(tmp_path):
    (tmp_path / _name(_utc(2026, 6, 1, 3, 0))).write_text("a")
    (tmp_path / _name(_utc(2026, 6, 3, 3, 0), gpg=True)).write_text("b")
    (tmp_path / "ignore.txt").write_text("x")
    out = bs.list_backups(str(tmp_path))
    assert [b["filename"] for b in out] == [
        _name(_utc(2026, 6, 3, 3, 0), gpg=True),
        _name(_utc(2026, 6, 1, 3, 0)),
    ]
    assert out[0]["encrypted"] is True
    assert out[1]["encrypted"] is False


# =============================================================================
# perform_backup — no-op + mocked pg_dump
# =============================================================================
def test_perform_backup_noop_when_destination_missing(db_session):
    bs.update_settings(db_session, destination="/nonexistent/backups/zzz")
    result = bs.perform_backup(db_session)
    assert result["ok"] is False
    assert result["status"] == "no_destination"
    # last_result recorded for the UI.
    cfg = bs.get_settings(db_session)
    assert cfg["last_result"]["status"] == "no_destination"
    assert cfg["last_run_at"] is not None


def test_perform_backup_success_mocked(db_session, tmp_path):
    bs.update_settings(db_session, destination=str(tmp_path), encrypt=False)

    def fake_run(cmd, **kwargs):
        # Emulate pg_dump writing to its stdout target.
        out = kwargs.get("stdout")
        if out is not None:
            out.write(b"FAKE PGDUMP DATA")
            out.flush()
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    with mock.patch("app.services.backup_service.subprocess.run", side_effect=fake_run) as m:
        result = bs.perform_backup(db_session)

    assert result["ok"] is True
    assert result["status"] == "success"
    assert result["filename"].startswith("opentranscribe-")
    assert result["filename"].endswith(".dump")
    assert result["size_bytes"] > 0
    # pg_dump invoked with custom format + a --dbname URL, no shell.
    called_cmd = m.call_args.args[0]
    assert called_cmd[0] == "pg_dump"
    assert "--format=custom" in called_cmd
    # The artifact actually exists on disk.
    assert (tmp_path / result["filename"]).is_file()


def test_perform_backup_records_error_on_pgdump_failure(db_session, tmp_path):
    bs.update_settings(db_session, destination=str(tmp_path))

    def boom(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr=b"connection refused")

    with mock.patch("app.services.backup_service.subprocess.run", side_effect=boom):
        result = bs.perform_backup(db_session)

    assert result["ok"] is False
    assert result["status"] == "error"
    assert "connection refused" in result["error"]
    # No partial dump left behind.
    assert list(Path(tmp_path).glob("*.dump")) == []


def test_perform_backup_encrypt_requires_passphrase(db_session, tmp_path):
    bs.update_settings(db_session, destination=str(tmp_path), encrypt=True, passphrase_file="")

    def fake_run(cmd, **kwargs):
        out = kwargs.get("stdout")
        if out is not None:
            out.write(b"DATA")
            out.flush()
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    with mock.patch("app.services.backup_service.subprocess.run", side_effect=fake_run):
        result = bs.perform_backup(db_session)

    # Encryption requested but no passphrase file → error, not a silent plaintext dump.
    assert result["ok"] is False
    assert result["status"] == "error"


def test_last_result_json_roundtrips(db_session):
    bs.update_settings(db_session, destination="/nonexistent/zzz")
    bs.perform_backup(db_session)
    from app.services import system_settings_service as sss

    raw = sss.get_setting(db_session, bs.KEY_LAST_RESULT)
    assert raw is not None  # perform_backup always records a result
    assert json.loads(raw)["status"] == "no_destination"


# =============================================================================
# S3 destination — settings, secret contract, retention, upload path
# =============================================================================
def test_s3_settings_defaults(db_session):
    cfg = bs.get_settings(db_session)
    assert cfg["destination_type"] == C.DEFAULT_BACKUP_DESTINATION_TYPE == "local"
    assert cfg["s3_bucket"] == ""
    assert cfg["s3_prefix"] == C.DEFAULT_BACKUP_S3_PREFIX == "opentranscribe/"
    assert cfg["s3_secret_key_set"] is False


def test_destination_type_validation(db_session):
    with pytest.raises(ValueError, match="destination_type"):
        bs.update_settings(db_session, destination_type="ftp")


def test_destination_type_switching(db_session):
    bs.update_settings(db_session, destination_type="s3", s3_bucket="mybucket")
    cfg = bs.get_settings(db_session)
    assert cfg["destination_type"] == "s3"
    assert cfg["s3_bucket"] == "mybucket"
    bs.update_settings(db_session, destination_type="local")
    assert bs.get_settings(db_session)["destination_type"] == "local"


def test_s3_secret_is_encrypted_and_never_echoed(db_session):
    bs.update_settings(db_session, s3_secret_key="super-secret-value")
    cfg = bs.get_settings(db_session)
    # The settings dict NEVER contains the secret — only a bool flag.
    assert "s3_secret_key" not in cfg
    assert cfg["s3_secret_key_set"] is True
    # Stored value is encrypted at rest (not the plaintext).
    raw = sss.get_setting(db_session, bs.KEY_S3_SECRET_KEY)
    assert raw is not None
    assert raw != "super-secret-value"
    # But the service can decrypt it back for runtime use.
    assert bs._get_s3_secret_key(db_session) == "super-secret-value"


def test_s3_secret_blank_leaves_unset_then_can_clear(db_session):
    # Setting a secret then clearing it with an empty string removes it.
    bs.update_settings(db_session, s3_secret_key="abc123")
    assert bs.get_settings(db_session)["s3_secret_key_set"] is True
    bs.update_settings(db_session, s3_secret_key="")
    assert bs.get_settings(db_session)["s3_secret_key_set"] is False


def test_select_to_delete_works_on_s3_keys():
    # The retention selector is backend-agnostic — feed it full object keys.
    prefix = "opentranscribe/"
    keys = [f"{prefix}{_name(_utc(2026, 6, d, 3, 0))}" for d in range(1, 11)]
    to_delete = bs.select_backups_to_delete(
        keys, retention_daily=7, retention_weekly=0, retention_monthly=0
    )
    assert len(to_delete) == 3
    assert f"{prefix}{_name(_utc(2026, 6, 1, 3, 0))}" in to_delete
    assert f"{prefix}{_name(_utc(2026, 6, 10, 3, 0))}" not in to_delete


class _FakeS3Client:
    """Minimal in-memory S3 stand-in for the boto3 client boundary."""

    def __init__(self, *, bucket_ok=True, objects=None):
        self._bucket_ok = bucket_ok
        # objects: {key: size}
        self.objects: dict[str, int] = dict(objects or {})
        self.uploaded: list[tuple[str, str]] = []
        self.deleted: list[str] = []

    def head_bucket(self, Bucket):  # noqa: N803 - boto3 kwarg name
        if not self._bucket_ok:
            raise RuntimeError("bucket not found")
        return {}

    def upload_file(self, local_path, bucket, key):
        size = Path(local_path).stat().st_size
        self.objects[key] = size
        self.uploaded.append((bucket, key))

    def delete_object(self, Bucket, Key):  # noqa: N803
        self.objects.pop(Key, None)
        self.deleted.append(Key)

    def list_objects_v2(self, **kwargs):
        prefix = kwargs.get("Prefix", "")
        contents = [{"Key": k, "Size": v} for k, v in self.objects.items() if k.startswith(prefix)]
        return {"Contents": contents}

    def get_paginator(self, _name):
        client = self

        class _Paginator:
            def paginate(self, **kwargs):
                yield client.list_objects_v2(**kwargs)

        return _Paginator()


def _pgdump_writes(payload=b"FAKE PGDUMP DATA"):
    def fake_run(cmd, **kwargs):
        out = kwargs.get("stdout")
        if out is not None:
            out.write(payload)
            out.flush()
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    return fake_run


def test_s3_backup_success_uploads_and_cleans_temp(db_session, tmp_path):
    bs.update_settings(
        db_session,
        destination_type="s3",
        destination=str(tmp_path),  # scratch dir for the temp dump
        s3_bucket="backups",
        s3_prefix="ot/",
        encrypt=False,
    )
    fake = _FakeS3Client()

    with (
        mock.patch("app.services.backup_service.subprocess.run", side_effect=_pgdump_writes()),
        mock.patch("app.services.backup_service._build_s3_client", return_value=fake),
    ):
        result = bs.perform_backup(db_session)

    assert result["ok"] is True
    assert result["status"] == "success"
    assert result["path"].startswith("s3://backups/ot/")
    # The object landed in the bucket under the prefix.
    assert len(fake.uploaded) == 1
    bucket, key = fake.uploaded[0]
    assert bucket == "backups"
    assert key.startswith("ot/opentranscribe-")
    assert key.endswith(".dump")
    # No temp dump left behind in the scratch dir.
    assert list(Path(tmp_path).glob("*.dump")) == []


def test_s3_backup_prunes_bucket(db_session, tmp_path):
    bs.update_settings(
        db_session,
        destination_type="s3",
        destination=str(tmp_path),
        s3_bucket="backups",
        s3_prefix="ot/",
        retention_daily=2,
        retention_weekly=0,
        retention_monthly=0,
    )
    # Seed 5 older daily backups already in the bucket.
    existing = {f"ot/{_name(_utc(2026, 6, d, 3, 0))}": 100 for d in range(1, 6)}
    fake = _FakeS3Client(objects=existing)

    with (
        mock.patch("app.services.backup_service.subprocess.run", side_effect=_pgdump_writes()),
        mock.patch("app.services.backup_service._build_s3_client", return_value=fake),
    ):
        result = bs.perform_backup(db_session)

    assert result["ok"] is True
    # daily=2 → only the 2 newest survive across (5 seeded + 1 new) = 6; 4 pruned.
    assert len(result["pruned"]) == 4
    assert len(fake.deleted) == 4


def test_s3_backup_no_bucket_is_graceful(db_session):
    bs.update_settings(db_session, destination_type="s3", s3_bucket="")
    result = bs.perform_backup(db_session)
    assert result["ok"] is False
    assert result["status"] == "no_destination"


def test_s3_backup_upload_failure_recorded_not_raised(db_session, tmp_path):
    bs.update_settings(
        db_session,
        destination_type="s3",
        destination=str(tmp_path),
        s3_bucket="backups",
    )

    class _Boom(_FakeS3Client):
        def upload_file(self, *a, **k):
            raise RuntimeError("network unreachable")

    with (
        mock.patch("app.services.backup_service.subprocess.run", side_effect=_pgdump_writes()),
        mock.patch("app.services.backup_service._build_s3_client", return_value=_Boom()),
    ):
        result = bs.perform_backup(db_session)

    assert result["ok"] is False
    assert result["status"] == "error"
    assert "network unreachable" in result["error"]
    # Temp dump cleaned up despite the failure.
    assert list(Path(tmp_path).glob("*.dump")) == []


def test_s3_bucket_status_graceful_on_failure(db_session):
    cfg = bs.get_settings(db_session)
    cfg["s3_bucket"] = "backups"
    with mock.patch(
        "app.services.backup_service._build_s3_client",
        return_value=_FakeS3Client(bucket_ok=False),
    ):
        status = bs.s3_bucket_status(cfg, db_session)
    assert status["reachable"] is False
    assert status["error"] is not None


def test_s3_connection_test_uses_override_secret(db_session):
    cfg = bs.get_settings(db_session)
    cfg["s3_bucket"] = "backups"
    fake = _FakeS3Client()
    with mock.patch("app.services.backup_service._build_s3_client", return_value=fake) as m:
        result = bs.test_s3_connection(cfg, db_session, override_secret="typed-secret")
    assert result["ok"] is True
    # The override secret was passed straight to the client builder (not the stored one).
    assert m.call_args.args[1] == "typed-secret"


def test_list_backups_s3_parses_and_sorts(db_session):
    cfg = bs.get_settings(db_session)
    cfg["s3_bucket"] = "backups"
    cfg["s3_prefix"] = "ot/"
    objects = {
        "ot/opentranscribe-20260601-030000.dump": 10,
        "ot/opentranscribe-20260603-030000.dump.gpg": 20,
        "ot/not-a-backup.txt": 5,
    }
    with mock.patch(
        "app.services.backup_service._build_s3_client",
        return_value=_FakeS3Client(objects=objects),
    ):
        listed = bs.list_backups_s3(cfg, db_session)
    # Unknown file ignored; newest first; prefix stripped from filename.
    assert len(listed) == 2
    assert listed[0]["filename"] == "opentranscribe-20260603-030000.dump.gpg"
    assert listed[0]["encrypted"] is True
    assert listed[1]["filename"] == "opentranscribe-20260601-030000.dump"
