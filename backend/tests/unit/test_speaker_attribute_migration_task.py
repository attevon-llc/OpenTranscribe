"""Tests for ``app/tasks/speaker_attribute_migration_task.py`` (issue #445).

Four things pinned here, none of which had a test before:

1. **A real, unhandled ``KeyError`` in ``_gender_result_writer``.** ``speaker_probs[sid]`` is
   seeded with only ``{"male": 0.0, "female": 0.0}`` (L133), and the per-segment update
   (L135) indexes it with whatever label the gender model returned. Any third label raises.
   Empirically, the raise happens **before** ``with session_scope()`` is even entered (the
   probability-accumulation loop at L130-L136 sits above L142's ``with``), so nothing is
   written for that file — no half-committed speakers. The exception is then caught one frame
   up, in ``migration_pipeline.process_batch_pipelined``'s per-file ``try/except`` (L364-L381
   there): only that one file is marked failed, the batch continues, and any file processed
   earlier in the same batch keeps its already-committed write. Confirmed by
   ``test_a_third_gender_label_raises_keyerror_before_any_db_session_is_opened`` (direct call)
   and ``test_the_keyerror_is_caught_per_file_so_only_that_file_fails`` (through the real
   pipeline).
2. **Every speaker is stamped, valid result or not.** The ``else`` branch (short/unusable
   audio — no segment survived extraction) still sets ``attributes_predicted_at = now`` with
   ``predicted_gender=None`` / ``attribute_confidence={"gender": 0.0}``. Because
   ``_get_files_needing_attribute_detection`` filters on
   ``Speaker.attributes_predicted_at.is_(None)``, that speaker is never offered again in
   non-force mode — pinned by re-running the query after the writer and checking the file
   drops out of it.
3. **The Redis orchestrator lock** (``r.set(lock_key, task_id, nx=True, ex=3600)``): a 1-hour
   TTL, and the ``except`` block's ``r.delete(lock_key)`` actually runs and releases the lock
   when an exception is raised anywhere inside the guarded ``try`` — forced here via
   ``send_ws_event``.
4. **The documented double-commit in force mode**: ``_reset_all_speaker_attributes`` commits
   explicitly, then the ``with session_scope()`` block that calls it commits again on normal
   exit. Pinned as harmless against a real (savepoint-backed) session — no exception, and the
   reset actually lands.

Following the characterization-test convention of ``tests/unit/test_transcription_storage.py``.
"""

from __future__ import annotations

import contextlib
import uuid as uuid_module
from datetime import UTC
from datetime import datetime
from typing import Any
from typing import cast

import pytest

from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.models.media import Speaker
from app.services.speaker_analysis_models import MultiModelRunner
from app.services.speaker_analysis_models import SegmentResult
from app.tasks import migration_pipeline
from app.tasks import speaker_attribute_migration_task as sat
from app.tasks.migration_pipeline import PreparedFile

# --------------------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------------------


def _media_file(db_session, user) -> MediaFile:
    mf = MediaFile(
        uuid=uuid_module.uuid4(),
        user_id=user.id,
        filename=f"clip-{uuid_module.uuid4().hex[:8]}.mp4",
        storage_path=f"user_{user.id}/{uuid_module.uuid4().hex}.mp4",
        file_size=1024,
        content_type="video/mp4",
        status=FileStatus.COMPLETED,
        duration=60.0,
    )
    db_session.add(mf)
    db_session.commit()
    db_session.refresh(mf)
    return mf


def _speaker(db_session, media_file: MediaFile, user, name: str, **extra: Any) -> Speaker:
    sp = Speaker(user_id=user.id, media_file_id=media_file.id, name=name, **extra)
    db_session.add(sp)
    db_session.commit()
    db_session.refresh(sp)
    return sp


def _prepared(media_file: MediaFile, user) -> PreparedFile:
    """A minimal PreparedFile — only ``media_file_id`` is read by ``_gender_result_writer``."""
    return PreparedFile(
        file_uuid=str(media_file.uuid),
        audio_source="https://example/presigned",
        speakers=[],
        speaker_segments={},
        media_file_id=media_file.id,
        user_id=user.id,
    )


@contextlib.contextmanager
def _scope_yielding(db_session):
    yield db_session


class FakeRedis:
    """Structural stand-in for the ``redis.Redis`` calls this module actually makes.

    Only ``set`` (with ``nx``/``ex``), ``get`` and ``delete`` — the module never calls
    anything else on the lock/status client.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.set_calls: list[dict[str, Any]] = []
        self.delete_calls: list[str] = []

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool | None:
        self.set_calls.append({"key": key, "value": value, "nx": nx, "ex": ex})
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def delete(self, *keys: str) -> int:
        for k in keys:
            self.delete_calls.append(k)
            self.store.pop(k, None)
        return len(keys)

    def ping(self) -> bool:
        return True


# --------------------------------------------------------------------------------------
# 1 & 2. _gender_result_writer — normal path, the stamp-everyone else branch, the KeyError
# --------------------------------------------------------------------------------------


def test_a_speaker_with_valid_results_gets_the_majority_label_and_averaged_confidence(
    monkeypatch, db_session, normal_user
):
    mf = _media_file(db_session, normal_user)
    sp = _speaker(db_session, mf, normal_user, "SPEAKER_00")

    results = {
        "gender": [
            SegmentResult(model_name="gender", speaker_id=sp.id, value=("male", 0.9)),
            SegmentResult(model_name="gender", speaker_id=sp.id, value=("male", 0.7)),
        ]
    }

    monkeypatch.setattr(sat, "session_scope", lambda: _scope_yielding(db_session))
    sat._gender_result_writer(_prepared(mf, normal_user), results)

    db_session.expire_all()
    refreshed = db_session.get(Speaker, sp.id)
    assert refreshed.predicted_gender == "male"
    assert refreshed.attribute_confidence == {"gender": 0.8}
    assert refreshed.attributes_predicted_at is not None


def test_a_speaker_with_no_usable_audio_is_still_stamped_and_becomes_unretryable(
    monkeypatch, db_session, normal_user
):
    """The 'else' branch: no segments -> None/0.0, but the file drops out of the retry query."""
    mf = _media_file(db_session, normal_user)
    sp = _speaker(db_session, mf, normal_user, "SPEAKER_00")
    assert sp.attributes_predicted_at is None

    # Sanity: before the writer runs, the file IS offered for non-force detection.
    pending_before = sat._get_files_needing_attribute_detection(db_session)
    assert mf.id in {f.id for f in pending_before}

    @contextlib.contextmanager
    def _scope():
        yield db_session

    monkeypatch.setattr(sat, "session_scope", _scope)

    count = sat._gender_result_writer(_prepared(mf, normal_user), {"gender": []})

    assert count == 0
    db_session.expire_all()
    refreshed = db_session.get(Speaker, sp.id)
    assert refreshed.predicted_gender is None
    assert refreshed.attribute_confidence == {"gender": 0.0}
    assert refreshed.attributes_predicted_at is not None, (
        "the docstring's intentional behaviour: stamped even with no valid probability"
    )

    pending_after = sat._get_files_needing_attribute_detection(db_session)
    assert mf.id not in {f.id for f in pending_after}, (
        "a stamped-but-unpredicted speaker must never be retried in non-force mode — "
        "the query filters on attributes_predicted_at.is_(None)"
    )


def test_a_third_gender_label_raises_keyerror_before_any_db_session_is_opened(
    monkeypatch, db_session, normal_user
):
    """DEFECT: speaker_attribute_migration_task.py L133/L135.

    ``speaker_probs[sid]`` is seeded with only 'male'/'female' keys; a third label from the
    model raises KeyError. session_scope is replaced with something that explodes, proving
    the raise happens in the probability-accumulation loop, above the ``with`` block — so no
    session is ever opened and nothing partial is written for this file.
    """
    mf = _media_file(db_session, normal_user)
    sp = _speaker(db_session, mf, normal_user, "SPEAKER_00")

    def _explode():
        raise AssertionError("must not open a DB session before the KeyError site")

    monkeypatch.setattr(sat, "session_scope", _explode)

    results = {
        "gender": [SegmentResult(model_name="gender", speaker_id=sp.id, value=("unknown", 0.5))]
    }

    with pytest.raises(KeyError):
        sat._gender_result_writer(_prepared(mf, normal_user), results)


def test_the_keyerror_is_caught_per_file_so_only_that_file_fails(
    monkeypatch, db_session, normal_user
):
    """Drives the REAL migration_pipeline.process_batch_pipelined + REAL _gender_result_writer.

    One good file, one file whose gender result carries a bogus label. Proves: the batch does
    not crash, the good file's write stands (already-committed work from earlier files in a
    batch is not touched by a later file's failure), and the bad file is left completely
    untouched — no half-written speaker — so it remains eligible for a future non-force run.
    ffmpeg/MinIO/GPU are bypassed entirely by faking the two pipeline seams that touch them.
    """
    mf_good = _media_file(db_session, normal_user)
    sp_good = _speaker(db_session, mf_good, normal_user, "SPEAKER_00")
    mf_bad = _media_file(db_session, normal_user)
    sp_bad = _speaker(db_session, mf_bad, normal_user, "SPEAKER_00")

    canned = {
        str(mf_good.uuid): {
            "gender": [
                SegmentResult(model_name="gender", speaker_id=sp_good.id, value=("female", 0.6))
            ]
        },
        str(mf_bad.uuid): {
            "gender": [
                SegmentResult(model_name="gender", speaker_id=sp_bad.id, value=("unknown", 0.5))
            ]
        },
    }

    def _fake_submit_segment_fetches(prepared, pool, min_duration=None, max_segments=None):
        return [(None, None, prepared.file_uuid)]

    def _fake_process_file_segments(runner, seg_futures):
        fuuid = seg_futures[0][2]
        return canned[fuuid]

    monkeypatch.setattr(migration_pipeline, "submit_segment_fetches", _fake_submit_segment_fetches)
    monkeypatch.setattr(migration_pipeline, "_process_file_segments", _fake_process_file_segments)

    @contextlib.contextmanager
    def _scope():
        yield db_session

    monkeypatch.setattr(sat, "session_scope", _scope)

    successes: list[str] = []
    failures: list[tuple[str, Exception | None]] = []

    success, failed = migration_pipeline.process_batch_pipelined(
        prepared_files=[
            (str(mf_good.uuid), _prepared(mf_good, normal_user)),
            (str(mf_bad.uuid), _prepared(mf_bad, normal_user)),
        ],
        # submit_segment_fetches/_process_file_segments are faked above and never
        # touch the runner for real GPU work — cast stands in for the real type.
        runner=cast(MultiModelRunner, None),
        result_writer=sat._gender_result_writer,
        is_running_check=lambda: True,
        on_file_success=successes.append,
        on_file_failure=lambda fuuid, exc: failures.append((fuuid, exc)),
        min_duration=1.0,
    )

    assert success == 1
    assert failed == 1
    assert successes == [str(mf_good.uuid)]
    assert len(failures) == 1
    assert failures[0][0] == str(mf_bad.uuid)
    assert isinstance(failures[0][1], KeyError), (
        "the batch caught the real KeyError, not something else"
    )

    db_session.expire_all()
    good_refreshed = db_session.get(Speaker, sp_good.id)
    bad_refreshed = db_session.get(Speaker, sp_bad.id)
    assert good_refreshed.attributes_predicted_at is not None, (
        "the earlier file's commit must stand"
    )
    assert good_refreshed.predicted_gender == "female"
    assert bad_refreshed.attributes_predicted_at is None, (
        "nothing partial written for the failed file"
    )

    still_pending = {f.id for f in sat._get_files_needing_attribute_detection(db_session)}
    assert mf_bad.id in still_pending, "the failed file must remain eligible for retry"
    assert mf_good.id not in still_pending


# --------------------------------------------------------------------------------------
# 3. Redis orchestrator lock — TTL and crash cleanup
# --------------------------------------------------------------------------------------


def test_lock_has_a_one_hour_ttl_and_is_released_when_an_exception_hits_mid_run(
    monkeypatch, db_session, normal_user
):
    mf = _media_file(db_session, normal_user)
    _speaker(db_session, mf, normal_user, "SPEAKER_00")

    fake_redis = FakeRedis()
    monkeypatch.setattr(sat.attribute_migration_progress, "_redis_client", fake_redis)

    @contextlib.contextmanager
    def _scope():
        yield db_session

    monkeypatch.setattr(sat, "session_scope", _scope)

    def _boom(*_a, **_kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(sat, "send_ws_event", _boom)

    sat.migrate_speaker_attributes_task.push_request(id="task-lock-test")
    try:
        result = sat.migrate_speaker_attributes_task.run(user_id=normal_user.id, force=False)
    finally:
        sat.migrate_speaker_attributes_task.pop_request()

    assert result == {"status": "error", "message": "boom"}

    lock_key = f"{sat.attribute_migration_progress.key_prefix}:orchestrator_lock"
    lock_set_calls = [c for c in fake_redis.set_calls if c["key"] == lock_key]
    assert len(lock_set_calls) == 1
    assert lock_set_calls[0]["nx"] is True
    assert lock_set_calls[0]["ex"] == 3600

    assert lock_key in fake_redis.delete_calls, "the except block must release the lock"
    assert lock_key not in fake_redis.store, "and the release must actually take effect"


# --------------------------------------------------------------------------------------
# 4. Force-mode reset — the documented double commit
# --------------------------------------------------------------------------------------


def test_force_reset_survives_the_session_scopes_own_commit_on_exit(db_session, normal_user):
    mf = _media_file(db_session, normal_user)
    already_predicted = _speaker(
        db_session,
        mf,
        normal_user,
        "SPEAKER_00",
        attributes_predicted_at=datetime.now(UTC),
        predicted_gender="male",
        attribute_confidence={"gender": 0.9},
    )
    never_predicted = _speaker(db_session, mf, normal_user, "SPEAKER_01")

    # _reset_all_speaker_attributes is unscoped by design (it resets EVERY speaker in the
    # table, not just this test's), and this runs against a live dev DB with real rows, so
    # the expected count must be measured, not assumed to be 1.
    baseline = db_session.query(Speaker).filter(Speaker.attributes_predicted_at.isnot(None)).count()

    @contextlib.contextmanager
    def _scope():
        # Mirrors the real session_scope: a normal exit also commits, AFTER
        # _reset_all_speaker_attributes has already committed once inside the block.
        yield db_session
        db_session.commit()

    with _scope() as db:
        reset_count = sat._reset_all_speaker_attributes(db)

    assert reset_count == baseline, "every non-NULL row is reset, including this test's own"

    db_session.expire_all()
    reset_speaker = db_session.get(Speaker, already_predicted.id)
    untouched_speaker = db_session.get(Speaker, never_predicted.id)

    assert reset_speaker.attributes_predicted_at is None
    assert reset_speaker.predicted_gender is None
    assert reset_speaker.attribute_confidence is None
    assert untouched_speaker.attributes_predicted_at is None
