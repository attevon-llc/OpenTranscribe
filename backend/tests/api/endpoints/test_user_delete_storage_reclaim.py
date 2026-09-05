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
from app.models.media import Speaker
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

    The OpenSearch leg is replaced by a **spy** so this measures the STORAGE leg
    exactly. It is not decoration: against an unreachable cluster —
    ``SKIP_OPENSEARCH=True`` with no OpenSearch service, which is precisely how the
    GitHub ``backend-tests`` job runs — ``_cleanup_opensearch_for_file`` contributes
    three more residuals of its own, and the exact assertions below became
    ``storage_objects_failed == 4`` / ``stages == ["storage", "transcript",
    "transcript_chunks", "transcript_summaries"]``. The spy still asserts the account
    path *routes through* that leg with this file's plan, so stubbing it cannot hide a
    fix that stopped calling it; its own residual reporting is covered against
    ``purge_media_file``.
    """
    import logging

    import app.services.file_cleanup_service as fcs
    import app.services.minio_service as minio_mod

    fuuid = uuid_pkg.uuid4()
    failing_key = f"tests/issue695/{fuuid}/orig.bin"
    _make_media_file(db_session, int(normal_user.id), uuid=fuuid, storage_path=failing_key)

    opensearch_calls: list[str] = []

    def _spy_opensearch(target: dict, file_uuid: str) -> list[dict]:
        opensearch_calls.append(file_uuid)
        return []

    monkeypatch.setattr(fcs, "_cleanup_opensearch_for_file", _spy_opensearch)

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

    assert opensearch_calls == [str(fuuid)], (
        "the account purge no longer routes this file through the OpenSearch erasure"
    )
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

    **This test's red on pre-fix master is not evidence of the defect.** It does go
    red there, but only on `KeyError: 'storage_objects_failed'` — a response key that
    did not exist yet — and the behaviour it asserts (a user with zero files deletes
    cleanly, touching no object) is already true on master, because there is nothing
    for the missing storage phase to act on. Its only job is to prove the real fix
    does not replace "do nothing" with "sweep a prefix": a ``delete_prefix`` call would
    pass every other test here while quietly deleting unrelated objects that happen to
    share the account's user-id-keyed prefix.
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


def _make_speaker(db_session, user_id: int, media_file_id: int, **kwargs) -> Speaker:
    speaker = Speaker(
        uuid=kwargs.pop("uuid", None) or uuid_pkg.uuid4(),
        user_id=user_id,
        media_file_id=media_file_id,
        name=kwargs.pop("name", "SPEAKER_00"),
        **kwargs,
    )
    db_session.add(speaker)
    db_session.commit()
    db_session.refresh(speaker)
    return speaker


def _make_speaker_profile(db_session, user_id: int, **kwargs) -> SpeakerProfile:
    profile = SpeakerProfile(
        uuid=kwargs.pop("uuid", None) or uuid_pkg.uuid4(),
        user_id=user_id,
        name=kwargs.pop("name", f"Profile {uuid_pkg.uuid4().hex[:8]}"),
        **kwargs,
    )
    db_session.add(profile)
    db_session.commit()
    db_session.refresh(profile)
    return profile


def test_speaker_and_profile_embeddings_are_removed_only_after_the_rows_are_committed(
    client, admin_token_headers, normal_user, db_session, monkeypatch
):
    """The issue #715 headline: no OpenSearch round trip while a transaction is open.

    ``_delete_user_speakers`` used to call ``remove_speaker_embedding`` once per
    speaker — and ``_delete_user_owned_records`` called ``remove_profile_embedding``
    once per profile — from INSIDE ``delete_admin_user``'s ``db.begin_nested()``
    savepoint. This spies on ``Session.commit`` and both OpenSearch removal functions
    into one ORDERED list and asserts every removal call follows the commit — the same
    shape as ``test_the_objects_are_removed_only_after_the_rows_are_committed`` above,
    which pins the same rule for object storage.
    """
    import app.services.opensearch_service as os_mod

    media_file = _make_media_file(db_session, int(normal_user.id))
    speaker = _make_speaker(db_session, int(normal_user.id), int(media_file.id))
    profile = _make_speaker_profile(db_session, int(normal_user.id))
    # Captured now: the request commits and deletes these rows, which expires the
    # ORM instances — reading .uuid off them AFTER the request raises ObjectDeletedError.
    speaker_uuid = str(speaker.uuid)
    profile_uuid = str(profile.uuid)

    order: list[str] = []
    real_commit = db_session.commit

    def _commit_spy():
        order.append("commit")
        return real_commit()

    def _speaker_spy(speaker_uuid: str):
        order.append(f"speaker:{speaker_uuid}")
        return True

    def _profile_spy(profile_uuid: str):
        order.append(f"profile:{profile_uuid}")
        return True

    monkeypatch.setattr(db_session, "commit", _commit_spy)
    monkeypatch.setattr(os_mod, "remove_speaker_embedding", _speaker_spy)
    monkeypatch.setattr(os_mod, "remove_profile_embedding", _profile_spy)

    response = client.delete(f"/api/admin/users/{normal_user.uuid}", headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK, response.text

    speaker_call = f"speaker:{speaker_uuid}"
    profile_call = f"profile:{profile_uuid}"

    assert "commit" in order
    assert speaker_call in order, f"the speaker embedding was never removed: {order}"
    assert profile_call in order, f"the profile embedding was never removed: {order}"
    assert order.index("commit") < order.index(speaker_call), (
        f"a speaker embedding was removed before any commit: {order}"
    )
    assert order.index("commit") < order.index(profile_call), (
        f"a profile embedding was removed before any commit: {order}"
    )


def test_a_speaker_embedding_removal_failure_is_reported_as_partial(
    client, admin_token_headers, normal_user, db_session, monkeypatch
):
    """A removal failure must land in ``residual_errors`` -> PARTIAL, never a 500 or a silent pass.

    ``purge_account_external_copies`` takes no session and must never raise
    (``users.delete_user`` has no try/except around it) — so this asserts the account
    delete still returns 200 with the deletion already committed, AND that the
    failure is visible in the audit trail, not merely logged.

    The file's own OpenSearch legs (transcript/chunks/summaries) and the speaker
    survivor-count verification are stubbed to a clean no-op — this test measures the
    speaker-removal failure in isolation, not whatever a real/unreachable cluster does
    to the file-level legs (see ``test_a_storage_failure_is_reported_and_does_not_
    silently_pass``'s docstring for why those legs are noisy without a stub).
    """
    import app.services.file_cleanup_service as fcs
    import app.services.opensearch_service as os_mod
    from app.services import account_security_service as acct_module

    media_file = _make_media_file(db_session, int(normal_user.id))
    _make_speaker(db_session, int(normal_user.id), int(media_file.id))

    def _boom(speaker_uuid: str):
        raise RuntimeError(f"simulated OpenSearch outage for {speaker_uuid}")

    monkeypatch.setattr(os_mod, "remove_speaker_embedding", _boom)
    monkeypatch.setattr(fcs, "_cleanup_opensearch_for_file", lambda target, file_uuid: [])
    monkeypatch.setattr(fcs, "_count_surviving", lambda index, query: 0)
    # The storage leg must SUCCEED for this test to measure what its docstring claims.
    # Left real, the result depends on whether object storage happens to be reachable:
    # locally the dev stack answers and only "speakers" fails, but CI has no MinIO, so
    # `delete_file_storage_artifacts` returns False and `stages` becomes
    # ["speakers", "storage"]. Stubbing at this seam (rather than at `minio_service.
    # delete_file`) is what actually covers it — the storage leg also touches the
    # derived-render cache bucket, so a lower-level stub leaves that path still live.
    monkeypatch.setattr(fcs, "delete_file_storage_artifacts", lambda file_id, meta: True)

    events: list[dict] = []
    real_log = acct_module.audit_logger.log

    def _spy_log(**kw):
        events.append(kw)
        return real_log(**kw)

    monkeypatch.setattr(acct_module.audit_logger, "log", _spy_log)

    response = client.delete(f"/api/admin/users/{normal_user.uuid}", headers=admin_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text

    db_session.expire_all()
    assert db_session.query(User).filter(User.id == normal_user.id).first() is None, (
        "a residual OpenSearch failure must not be retried by leaving the account undeleted"
    )

    assert len(events) == 1
    assert events[0]["outcome"] is AuditOutcome.PARTIAL
    assert events[0]["details"]["stages"] == ["speakers"]


def _calls(func_node: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> bool:
    """True when ``func_node``'s body calls ``name`` (bare or as an attribute)."""
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


def _cascade_callers_missing_purge(root: Path) -> list[str]:
    """Every function under ``root`` that runs the cascade without the storage purge.

    Module level, and taking a root, so the must-fire control below runs **this**
    detector over a synthetic tree rather than re-implementing it — a control that
    re-implements the thing it is controlling proves only that the copy works.
    """
    offenders: list[str] = []
    for py_file in sorted(root.rglob("*.py")):
        # No `except SyntaxError`: every file under backend/app is real, importable
        # application code, so a parse failure here is itself a finding worth
        # seeing, not something to skip past.
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if _calls(node, "_delete_user_media_files") and not _calls(
                    node, "purge_account_external_copies"
                ):
                    offenders.append(f"{py_file.relative_to(root)}::{node.name}")
    return offenders


def test_every_caller_of_the_user_cascade_also_purges_storage():
    """Structural backstop: any caller of ``_delete_user_media_files`` must also purge storage.

    AST over ``backend/app``. Its must-fire control is the test below, which drives the
    same detector over a synthetic offender so this cannot be a detector matching
    nothing.
    """
    app_root = Path(__file__).resolve().parents[3] / "app"
    assert app_root.is_dir(), app_root

    offenders = _cascade_callers_missing_purge(app_root)

    assert offenders == [], (
        "every caller of _delete_user_media_files must also call "
        f"purge_account_external_copies (issue #695): {offenders}"
    )


def test_the_structural_backstop_fires_on_a_synthetic_offender(tmp_path):
    """Must-fire AND must-stay-clean control for the AST backstop above.

    Without this the detector could match nothing and the test above would pass
    vacuously forever. It runs ``_cascade_callers_missing_purge`` itself over a
    two-file tree: one function that calls the cascade alone (must be reported) and
    one that also purges (must not be).
    """
    fake_app = tmp_path / "app"
    fake_app.mkdir()
    (fake_app / "offender.py").write_text(
        "def bad_caller(db, user_id):\n    _delete_user_media_files(db, user_id)\n"
    )
    (fake_app / "compliant.py").write_text(
        "def good_caller(db, user_id, plan):\n"
        "    _delete_user_media_files(db, user_id)\n"
        "    purge_account_external_copies(plan)\n"
    )

    assert _cascade_callers_missing_purge(fake_app) == ["offender.py::bad_caller"]
