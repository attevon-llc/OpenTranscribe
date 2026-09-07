"""Unit tests for ``app/scripts/fetch_backup.py`` (issue #600).

An S3-destination scheduled backup exists ONLY in the bucket — ``_perform_backup_s3``
always deletes the local artifact after upload. The S3 credentials to fetch it back are
AES-256-GCM encrypted in ``SystemSettings``, decryptable only inside the backend
container, and ☠️ they live in the very database ``./opentr.sh restore`` is about to drop
— so the fetch step MUST run, and complete, before anything destructive. This file covers
the fetch logic itself (against the in-memory ``FakeS3Client``, extracted to
``tests/fixtures/fake_s3.py`` specifically because this is now its second consumer); the
real S3-round-trip proof lives in ``tests/integration/test_scheduled_backup_restore_roundtrip.py``.

Same DB-backed pattern as ``test_backup_service.py`` (savepoint-rolled-back ``db_session``,
``xdist_group("backup_system_settings")`` — both files upsert the same ``backup.*``
SystemSettings keys with no coordination, so a shared group avoids the
``system_settings_key_key`` unique-index deadlock under ``-n auto``, issue #389).
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from app.scripts import fetch_backup as fb
from app.services import backup_service as bs
from tests.fixtures.fake_s3 import FakeS3Client

pytestmark = pytest.mark.xdist_group("backup_system_settings")


# ---------------------------------------------------------------------------------------------
# _looks_like_our_artifact — the magic-byte sanity check.
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_looks_like_our_artifact_accepts_a_real_dump_prefix() -> None:
    assert fb._looks_like_our_artifact(b"PGDMP" + b"\x00" * 10, "opentranscribe-x.dump")


@pytest.mark.unit
def test_looks_like_our_artifact_accepts_a_gpg_envelope_prefix() -> None:
    # A real OpenPGP packet header always has its high bit set (RFC 4880 4.2) — 0xC3 is a
    # plausible new-format symmetric-key-encrypted-session-key packet tag.
    assert fb._looks_like_our_artifact(b"\xc3\x01\x02\x03\x04", "opentranscribe-x.dump.gpg")


@pytest.mark.unit
def test_looks_like_our_artifact_rejects_empty_body() -> None:
    """Must-fire control: a truncated-to-zero download."""
    assert not fb._looks_like_our_artifact(b"", "opentranscribe-x.dump")


@pytest.mark.unit
def test_looks_like_our_artifact_rejects_junk_text() -> None:
    """Must-fire control: a plain-text body (not PGDMP, not a real OpenPGP header)."""
    assert not fb._looks_like_our_artifact(b"junk-not-a-dump", "opentranscribe-x.dump")


@pytest.mark.unit
def test_looks_like_our_artifact_rejects_an_xml_error_body() -> None:
    """Must-fire control: the shape a misrouted/misconfigured S3 request commonly returns."""
    assert not fb._looks_like_our_artifact(b"<?xml version='1.0'?><Error>...</Error>", "x.dump")


# ---------------------------------------------------------------------------------------------
# _fetch — the real download path against a FakeS3Client.
# ---------------------------------------------------------------------------------------------


def _configure_s3(db_session, *, destination: str) -> None:
    bs.update_settings(
        db_session,
        destination_type="s3",
        destination=destination,
        s3_bucket="backups",
        s3_prefix="ot/",
    )


@pytest.mark.unit
def test_fetch_downloads_into_the_configured_destination(db_session, tmp_path: Path) -> None:
    dest = tmp_path / "backups"
    dest.mkdir()
    _configure_s3(db_session, destination=str(dest))
    cfg = bs.get_settings(db_session)

    body = b"PGDMP" + b"\x00" * 100
    fake = FakeS3Client(contents={"ot/opentranscribe-20260827-030000.dump": body})

    with mock.patch("app.services.backup_service._build_s3_client", return_value=fake):
        target = fb._fetch(cfg, None, "opentranscribe-20260827-030000.dump", force=False)

    assert target == dest / "opentranscribe-20260827-030000.dump"
    assert target.read_bytes() == body
    assert not target.with_name(target.name + ".part").exists()


@pytest.mark.unit
def test_fetch_refuses_when_destination_type_is_not_s3(db_session, tmp_path: Path) -> None:
    bs.update_settings(db_session, destination_type="local", destination=str(tmp_path))
    cfg = bs.get_settings(db_session)

    with pytest.raises(fb.FetchError, match="not 's3'"):
        fb._fetch(cfg, None, "opentranscribe-x.dump", force=False)


@pytest.mark.unit
def test_fetch_refuses_when_destination_is_not_writable(db_session, tmp_path: Path) -> None:
    unwritable = tmp_path / "does-not-exist"
    _configure_s3(db_session, destination=str(unwritable))
    cfg = bs.get_settings(db_session)

    with pytest.raises(fb.FetchError, match="not a writable mount"):
        fb._fetch(cfg, None, "opentranscribe-x.dump", force=False)


@pytest.mark.unit
def test_fetch_refuses_to_overwrite_an_existing_file_without_force(
    db_session, tmp_path: Path
) -> None:
    dest = tmp_path / "backups"
    dest.mkdir()
    (dest / "opentranscribe-x.dump").write_bytes(b"already here")
    _configure_s3(db_session, destination=str(dest))
    cfg = bs.get_settings(db_session)

    fake = FakeS3Client(contents={"ot/opentranscribe-x.dump": b"PGDMP" + b"\x00" * 10})
    with (
        mock.patch("app.services.backup_service._build_s3_client", return_value=fake),
        pytest.raises(fb.FetchError, match="already exists"),
    ):
        fb._fetch(cfg, None, "opentranscribe-x.dump", force=False)


@pytest.mark.unit
def test_fetch_force_overwrites_an_existing_file(db_session, tmp_path: Path) -> None:
    dest = tmp_path / "backups"
    dest.mkdir()
    (dest / "opentranscribe-x.dump").write_bytes(b"stale content")
    _configure_s3(db_session, destination=str(dest))
    cfg = bs.get_settings(db_session)

    fresh_body = b"PGDMP" + b"\x00" * 20
    fake = FakeS3Client(contents={"ot/opentranscribe-x.dump": fresh_body})
    with mock.patch("app.services.backup_service._build_s3_client", return_value=fake):
        target = fb._fetch(cfg, None, "opentranscribe-x.dump", force=True)

    assert target.read_bytes() == fresh_body


@pytest.mark.unit
def test_fetch_rejects_a_truncated_download(db_session, tmp_path: Path) -> None:
    """Must-fire: the object's real body is shorter than S3 reported (ContentLength)."""
    dest = tmp_path / "backups"
    dest.mkdir()
    _configure_s3(db_session, destination=str(dest))
    cfg = bs.get_settings(db_session)

    fake = FakeS3Client(contents={"ot/opentranscribe-x.dump": b"PGDMP" + b"\x00" * 100})
    # Lie about the size after construction — simulates a download that stops partway
    # through despite S3's head_object reporting the full length.
    fake.objects["ot/opentranscribe-x.dump"] = 1_000_000

    with (
        mock.patch("app.services.backup_service._build_s3_client", return_value=fake),
        pytest.raises(fb.FetchError, match="truncated|interrupted"),
    ):
        fb._fetch(cfg, None, "opentranscribe-x.dump", force=False)

    assert not (dest / "opentranscribe-x.dump").exists(), (
        "a truncated download must not be left at the final path"
    )
    assert not (dest / "opentranscribe-x.dump.part").exists(), (
        "a truncated download must not leave a stray .part file behind either"
    )


@pytest.mark.unit
def test_fetch_rejects_a_body_without_our_magic_bytes(db_session, tmp_path: Path) -> None:
    """Must-fire: the downloaded body doesn't look like a pg_dump/gpg artifact at all."""
    dest = tmp_path / "backups"
    dest.mkdir()
    _configure_s3(db_session, destination=str(dest))
    cfg = bs.get_settings(db_session)

    junk = b"not a database dump at all, just some junk bytes"
    fake = FakeS3Client(contents={"ot/opentranscribe-x.dump": junk})

    with (
        mock.patch("app.services.backup_service._build_s3_client", return_value=fake),
        pytest.raises(fb.FetchError, match="does not look like"),
    ):
        fb._fetch(cfg, None, "opentranscribe-x.dump", force=False)

    assert not (dest / "opentranscribe-x.dump").exists()


@pytest.mark.unit
def test_fetch_refuses_when_no_bucket_configured(db_session, tmp_path: Path) -> None:
    dest = tmp_path / "backups"
    dest.mkdir()
    bs.update_settings(db_session, destination_type="s3", destination=str(dest), s3_bucket="")
    cfg = bs.get_settings(db_session)

    with pytest.raises(fb.FetchError, match="no bucket configured"):
        fb._fetch(cfg, None, "opentranscribe-x.dump", force=False)


# ---------------------------------------------------------------------------------------------
# --list
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_do_list_matches_list_backups_s3(db_session, tmp_path: Path, capsys) -> None:
    dest = tmp_path / "backups"
    dest.mkdir()
    _configure_s3(db_session, destination=str(dest))
    cfg = bs.get_settings(db_session)

    fake = FakeS3Client(
        contents={"ot/opentranscribe-20260827-030000.dump": b"PGDMP" + b"\x00" * 10}
    )
    with mock.patch("app.services.backup_service._build_s3_client", return_value=fake):
        expected = bs.list_backups_s3(cfg, db_session)
        exit_code = fb._do_list(cfg)

    assert exit_code == 0
    printed = capsys.readouterr().out
    assert "opentranscribe-20260827-030000.dump" in printed
    import json

    parsed = json.loads(printed)
    assert parsed == expected


# ---------------------------------------------------------------------------------------------
# Secret hygiene — mirrors test_backup_service.py's test_no_key_material_in_logs.
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_secret_never_printed_or_logged(db_session, tmp_path: Path, capsys, caplog) -> None:
    dest = tmp_path / "backups"
    dest.mkdir()
    bs.update_settings(
        db_session,
        destination_type="s3",
        destination=str(dest),
        s3_bucket="backups",
        s3_prefix="ot/",
        s3_secret_key="SENTINEL-S3-SECRET-VALUE",
    )
    cfg = bs.get_settings(db_session)
    # The real decrypted secret, exactly as main() resolves it — a fetch() called with
    # None here would make this test pass trivially (nothing to leak).
    secret = bs._get_s3_secret_key(db_session)
    assert secret == "SENTINEL-S3-SECRET-VALUE", "test setup didn't actually store the secret"

    fake = FakeS3Client(contents={"ot/opentranscribe-x.dump": b"PGDMP" + b"\x00" * 10})
    with (
        caplog.at_level("DEBUG"),
        mock.patch("app.services.backup_service._build_s3_client", return_value=fake),
    ):
        fb._fetch(cfg, secret, "opentranscribe-x.dump", force=False)

    printed = capsys.readouterr().out
    assert "SENTINEL-S3-SECRET-VALUE" not in printed
    assert "SENTINEL-S3-SECRET-VALUE" not in caplog.text


# ---------------------------------------------------------------------------------------------
# main() — argument handling.
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_main_requires_a_filename_unless_listing() -> None:
    assert fb.main([]) == 2
