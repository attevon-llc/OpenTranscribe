"""Deleting a user must reclaim their object storage, not just their rows (issue #695).

``_delete_user_media_files`` (``admin.py``) ends in a bulk ``query(MediaFile).delete()``
that never touches object storage — every deleted account's recordings, thumbnails,
derived-cache renders and speaker-profile avatars survived in MinIO forever, and the
retention sweep can never find them because the row that named them is gone.

The fix: ``file_cleanup_service.load_account_purge_plans`` reads the storage plan
before the bulk delete, and ``purge_account_external_copies`` destroys the objects
after the transaction commits, with no transaction open (mirroring ``purge_media_file``'s
own phase split).

Two harness facts govern the tests below:

* ``db_session`` rolls back Postgres but NOT MinIO/OpenSearch — every uploaded test
  object is cleaned up in a ``finally``.
* In CI, object storage is unreachable but the client is REAL — tests needing live
  MinIO are gated by ``S3_LIVE`` (the same pattern as ``tests/unit/test_multipart_upload.py``).
"""

from __future__ import annotations

import ast
import os
import uuid as uuid_pkg
from pathlib import Path

import pytest
from fastapi import status
from minio.error import S3Error

from app.auth.audit import AuditOutcome
from app.core.config import settings
from app.models.media import MediaFile
from app.models.media import SpeakerProfile
from app.models.user import User
from app.services.minio_service import minio_client

S3_LIVE = os.environ.get("SKIP_S3", "True").lower() != "true"

pytestmark = pytest.mark.xdist_group("storage_backend")


def _put(bucket: str, key: str, body: bytes = b"test-object-695") -> None:
    """Upload real bytes to a real bucket, creating the bucket if needed."""
    import io

    if not minio_client.bucket_exists(bucket):
        minio_client.make_bucket(bucket)
    minio_client.put_object(bucket, key, io.BytesIO(body), length=len(body))


def _exists(bucket: str, key: str) -> bool:
    try:
        minio_client.stat_object(bucket, key)
        return True
    except S3Error:
        return False


def _cleanup(bucket: str, key: str) -> None:
    from contextlib import suppress

    with suppress(Exception):
        minio_client.remove_object(bucket, key)


def _make_media_file(db_session, user_id: int, **kwargs) -> MediaFile:
    fuuid = kwargs.pop("uuid", None) or uuid_pkg.uuid4()
    media_file = MediaFile(
        uuid=fuuid,
        filename=kwargs.pop("filename", f"issue695-{fuuid.hex[:8]}.mp4"),
        storage_path=kwargs.pop("storage_path", f"tests/issue695/{fuuid}/orig.bin"),
        content_type="video/mp4",
        file_size=1234,
        user_id=user_id,
        status="completed",
        **kwargs,
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


@pytest.mark.skipif(not S3_LIVE, reason="needs a real MinIO (SKIP_S3=False)")
def test_deleting_a_user_removes_their_media_object_from_real_storage(
    client, admin_token_headers, normal_user, admin_user, db_session
):
    """The headline defect: the object survives the row's deletion. Not anymore."""
    bucket = settings.MEDIA_BUCKET_NAME
    fuuid = uuid_pkg.uuid4()
    original_key = f"tests/issue695/{fuuid}/orig.bin"
    thumb_key = f"tests/issue695/{fuuid}/thumb.jpg"
    bystander_key = f"tests/issue695/bystander-{uuid_pkg.uuid4()}/orig.bin"

    _put(bucket, original_key)
    _put(bucket, thumb_key)
    _put(bucket, bystander_key)

    try:
        _make_media_file(
            db_session,
            int(normal_user.id),
            uuid=fuuid,
            storage_path=original_key,
            thumbnail_path=thumb_key,
        )
        # Bystander file belongs to a DIFFERENT user and must survive.
        _make_media_file(db_session, int(admin_user.id), storage_path=bystander_key)

        response = client.delete(
            f"/api/admin/users/{normal_user.uuid}", headers=admin_token_headers
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.json()["storage_objects_failed"] == 0

        assert not _exists(bucket, original_key), "original object survived the account delete"
        assert not _exists(bucket, thumb_key), "thumbnail object survived the account delete"
        assert _exists(bucket, bystander_key), "an UNRELATED user's object was deleted"
    finally:
        _cleanup(bucket, original_key)
        _cleanup(bucket, thumb_key)
        _cleanup(bucket, bystander_key)


@pytest.mark.skipif(not S3_LIVE, reason="needs a real MinIO (SKIP_S3=False)")
def test_the_users_router_delete_removes_the_objects_too(
    client, admin_token_headers, normal_user, db_session
):
    """``DELETE /api/users/{uuid}`` is the second caller — #689 had the same two-caller shape."""
    bucket = settings.MEDIA_BUCKET_NAME
    fuuid = uuid_pkg.uuid4()
    original_key = f"tests/issue695/{fuuid}/orig.bin"

    _put(bucket, original_key)

    try:
        _make_media_file(db_session, int(normal_user.id), uuid=fuuid, storage_path=original_key)

        response = client.delete(f"/api/users/{normal_user.uuid}", headers=admin_token_headers)
        assert response.status_code == status.HTTP_204_NO_CONTENT, response.text

        assert not _exists(bucket, original_key)
    finally:
        _cleanup(bucket, original_key)


@pytest.mark.skipif(not S3_LIVE, reason="needs a real MinIO (SKIP_S3=False)")
def test_the_derived_cache_renders_are_removed(
    client, admin_token_headers, normal_user, db_session
):
    """The most discriminating test: a storage_path-only fix leaves this behind.

    Thumbnails and ``storage_path`` are named on the row; the derived-cache burned-in
    render is NOT — its fingerprinted variant can only be found by LISTING the bucket
    (``VideoProcessingService._masked_video_cache_keys``). A fix that deletes only
    ``storage_path`` and ``thumbnail_path`` passes every other test here and still
    leaves a video of the transcript in storage.
    """
    bucket = "processed-videos"
    fuuid = uuid_pkg.uuid4()
    original_key = f"tests/issue695/{fuuid}/orig.bin"

    media_file = _make_media_file(
        db_session,
        int(normal_user.id),
        uuid=fuuid,
        storage_path=original_key,
        filename="issue695.mp4",
    )
    file_id = int(media_file.id)
    base = "issue695"

    plain_key = f"derived/{file_id}_{base}_with_speakers.mp4"
    fingerprinted_key = f"derived/{file_id}_{base}_with_speakers_rab12cd.mp4"

    other_media_file = _make_media_file(
        db_session, int(normal_user.id), storage_path=f"tests/issue695/{uuid_pkg.uuid4()}/orig.bin"
    )
    other_media_file_id = int(other_media_file.id)
    neighbour_key = f"derived/{other_media_file_id}_{base}_with_speakers.mp4"

    _put(bucket, plain_key)
    _put(bucket, fingerprinted_key)
    _put(bucket, neighbour_key)

    try:
        response = client.delete(
            f"/api/admin/users/{normal_user.uuid}", headers=admin_token_headers
        )
        assert response.status_code == status.HTTP_200_OK, response.text

        assert not _exists(bucket, plain_key), "unfingerprinted derived render survived"
        assert not _exists(bucket, fingerprinted_key), "fingerprinted derived render survived"
        assert _exists(bucket, neighbour_key), (
            "a NEIGHBOURING file's derived render was destroyed — the prefix must be "
            "id-scoped, not filename-scoped"
        )
    finally:
        _cleanup(bucket, plain_key)
        _cleanup(bucket, fingerprinted_key)
        _cleanup(bucket, neighbour_key)


@pytest.mark.skipif(not S3_LIVE, reason="needs a real MinIO (SKIP_S3=False)")
def test_the_speaker_profile_avatar_goes_with_the_account(
    client, admin_token_headers, normal_user, db_session
):
    """``SpeakerProfile.avatar_path`` is orphaned by the ADMIN path too (issue #695)."""
    bucket = settings.MEDIA_BUCKET_NAME
    avatar_key = f"tests/issue695/avatars/{uuid_pkg.uuid4()}.jpg"
    _put(bucket, avatar_key)

    profile = SpeakerProfile(
        uuid=uuid_pkg.uuid4(),
        user_id=int(normal_user.id),
        name=f"Profile {uuid_pkg.uuid4().hex[:8]}",
        avatar_path=avatar_key,
    )
    db_session.add(profile)
    db_session.commit()

    try:
        response = client.delete(
            f"/api/admin/users/{normal_user.uuid}", headers=admin_token_headers
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        assert not _exists(bucket, avatar_key)
    finally:
        _cleanup(bucket, avatar_key)


def test_a_storage_failure_is_reported_and_does_not_silently_pass(
    client, admin_token_headers, normal_user, db_session, monkeypatch, caplog
):
    """A failed delete is NOT retryable (the row is already gone) so it must be visible.

    No live MinIO needed: ``minio_service.delete_file`` is monkeypatched to raise for
    the file's key, which is enough to exercise the residual-error plumbing end to end.
    """
    import logging

    import app.services.minio_service as minio_mod

    fuuid = uuid_pkg.uuid4()
    failing_key = f"tests/issue695/{fuuid}/orig.bin"
    _make_media_file(db_session, int(normal_user.id), uuid=fuuid, storage_path=failing_key)

    real_delete_file = minio_mod.delete_file

    def _boom(object_name: str):
        if object_name == failing_key:
            raise RuntimeError(f"simulated MinIO outage for {object_name}")
        return real_delete_file(object_name)

    monkeypatch.setattr(minio_mod, "delete_file", _boom)

    from app.services import account_security_service as acct_module

    events: list[dict] = []
    real_log = acct_module.audit_logger.log

    def _spy_log(**kw):
        events.append(kw)
        return real_log(**kw)

    monkeypatch.setattr(acct_module.audit_logger, "log", _spy_log)

    with caplog.at_level(logging.WARNING):
        response = client.delete(
            f"/api/admin/users/{normal_user.uuid}", headers=admin_token_headers
        )

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["storage_objects_failed"] == 1

    db_session.expire_all()
    assert db_session.query(User).filter(User.id == normal_user.id).first() is None
    assert db_session.query(MediaFile).filter(MediaFile.storage_path == failing_key).first() is None

    assert len(events) == 1
    assert events[0]["outcome"] is AuditOutcome.PARTIAL
    assert events[0]["details"]["storage_objects_failed"] == 1
    assert events[0]["details"]["stages"] == ["storage"]
    # No object key in the audit `details` (mirrors the GDPR erasure ledger's no-free-text
    # rule) — the key belongs only in the ERROR log line.
    assert failing_key not in str(events[0]["details"])
    assert any(failing_key in record.message for record in caplog.records)


def test_a_user_with_no_files_still_deletes_cleanly(
    client, admin_token_headers, normal_user, monkeypatch
):
    """CONTROL against a plausible wrong fix (a prefix sweep), not a red/green pin.

    This test CANNOT be watched red against HEAD: a user with zero files already
    deletes cleanly on master, because there is nothing for the (missing) storage
    phase to fail on. Its only job is to prove the real fix does not replace "do
    nothing" with "sweep a prefix" — a `delete_prefix` call would still pass every
    other test here while quietly deleting unrelated objects that happen to share
    the account's user-id-keyed prefix.
    """
    import app.services.minio_service as minio_mod

    calls: list[str] = []
    monkeypatch.setattr(minio_mod, "delete_file", lambda path: calls.append(path))

    response = client.delete(f"/api/admin/users/{normal_user.uuid}", headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["storage_objects_failed"] == 0
    assert calls == []


def test_the_objects_are_removed_only_after_the_rows_are_committed(
    client, admin_token_headers, normal_user, db_session, monkeypatch
):
    """Pins the phase boundary: storage deletes only follow a commit.

    Under ``db_session`` this asserts CALL ORDER only, not durability — the savepoint
    harness intercepts real commits, so this cannot observe whether Postgres itself
    made the row-delete durable, only that the code path calls ``commit()`` before it
    calls ``delete_file()``.
    """
    import app.services.minio_service as minio_mod

    fuuid = uuid_pkg.uuid4()
    key = f"tests/issue695/{fuuid}/orig.bin"
    _make_media_file(db_session, int(normal_user.id), uuid=fuuid, storage_path=key)

    order: list[str] = []
    real_commit = db_session.commit

    def _commit_spy():
        order.append("commit")
        return real_commit()

    def _delete_spy(path):
        order.append(f"delete:{path}")

    monkeypatch.setattr(db_session, "commit", _commit_spy)
    monkeypatch.setattr(minio_mod, "delete_file", _delete_spy)

    response = client.delete(f"/api/admin/users/{normal_user.uuid}", headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK, response.text

    assert "commit" in order
    assert f"delete:{key}" in order
    assert order.index("commit") < order.index(f"delete:{key}"), (
        f"a storage delete happened before any commit: {order}"
    )


def test_the_media_file_delete_is_still_one_statement(
    client, admin_token_headers, normal_user, db_session
):
    """Pins decision 1: collect-plans-then-bulk-delete, never a purge_media_file loop.

    ``purge_media_file`` commits twice per file, which would release
    ``delete_admin_user``'s savepoint on the FIRST file if it were called in a loop —
    so the bulk ``query(MediaFile).delete()`` must survive as exactly one DELETE
    statement against ``media_file``, seeded with several rows so a per-row loop
    would be caught.
    """
    from sqlalchemy import event

    for _ in range(3):
        _make_media_file(db_session, int(normal_user.id))

    statements: list[str] = []

    def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        if "DELETE FROM media_file" in statement:
            statements.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", _before_cursor_execute)
    try:
        response = client.delete(
            f"/api/admin/users/{normal_user.uuid}", headers=admin_token_headers
        )
        assert response.status_code == status.HTTP_200_OK, response.text
    finally:
        event.remove(engine, "before_cursor_execute", _before_cursor_execute)

    assert len(statements) == 1, f"expected exactly one bulk DELETE, got {statements}"


def test_every_caller_of_the_user_cascade_also_purges_storage():
    """Structural backstop: any caller of ``_delete_user_media_files`` must also purge storage.

    AST over ``backend/app`` — includes a must-fire fixture (a synthetic function
    calling the cascade helper alone) so this cannot be a detector that matches
    nothing.
    """
    app_root = Path(__file__).resolve().parents[3] / "app"
    assert app_root.is_dir(), app_root

    offenders: list[str] = []

    def _calls(func_node: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> bool:
        for node in ast.walk(func_node):
            if isinstance(node, ast.Call):
                target = node.func
                called_name = None
                if isinstance(target, ast.Name):
                    called_name = target.id
                elif isinstance(target, ast.Attribute):
                    called_name = target.attr
                if called_name == name:
                    return True
        return False

    for py_file in app_root.rglob("*.py"):
        # No `except SyntaxError`: every file under backend/app is real, importable
        # application code, so a parse failure here is itself a finding worth
        # seeing, not something to skip past.
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if _calls(node, "_delete_user_media_files") and not _calls(
                    node, "purge_account_external_copies"
                ):
                    offenders.append(f"{py_file.relative_to(app_root)}::{node.name}")

    assert offenders == [], (
        "every caller of _delete_user_media_files must also call "
        f"purge_account_external_copies (issue #695): {offenders}"
    )


def test_the_structural_backstop_fires_on_a_synthetic_offender(tmp_path, monkeypatch):
    """Must-fire control for the AST backstop above.

    Without this, the detector could match nothing and the test above would pass
    vacuously forever.
    """
    offender_src = "def bad_caller(db, user_id):\n    _delete_user_media_files(db, user_id)\n"
    fake_app = tmp_path / "app"
    fake_app.mkdir()
    (fake_app / "offender.py").write_text(offender_src)

    import ast as ast_mod

    tree = ast_mod.parse(offender_src)
    found_bad_call = False
    found_good_call = False
    for node in ast_mod.walk(tree):
        if isinstance(node, ast_mod.Call) and isinstance(node.func, ast_mod.Name):
            if node.func.id == "_delete_user_media_files":
                found_bad_call = True
            if node.func.id == "purge_account_external_copies":
                found_good_call = True
    assert found_bad_call
    assert not found_good_call
