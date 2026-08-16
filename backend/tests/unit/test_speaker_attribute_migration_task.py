"""Tests for ``app/tasks/speaker_attribute_migration_task.py`` (issue #445).

Four things pinned here, none of which had a test before:

1. **FIX (was a real, unhandled ``KeyError``) in ``_gender_result_writer``.**
   ``speaker_probs[sid]`` is seeded with only ``{"male": 0.0, "female": 0.0}``, and the
   per-segment update used to index it with whatever label the gender model returned — any
   third label raised. The code now only accumulates confidence for a label it understands
   (``"male"``/``"female"``); an unexpected label is logged via ``logger.warning`` (naming the
   label, speaker, and file) and skipped, while any other valid results already accumulated
   for that same speaker — in the same file or a different one — still count normally. The
   file no longer fails in ``migration_pipeline.process_batch_pipelined``'s per-file
   ``try/except`` for this reason. Confirmed by
   ``test_an_unexpected_gender_label_is_skipped_with_a_warning_not_raised`` (direct call) and
   ``test_the_unexpected_label_no_longer_fails_the_file_in_the_pipeline`` (through the real
   pipeline). ``test_a_speaker_with_some_valid_results_and_one_unexpected_label_still_gets_a_
   correct_majority`` pins that a single bad-label result does not blank out an otherwise
   good aggregation.
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
import logging
import uuid as uuid_module
from datetime import UTC
from datetime import datetime
from typing import Any
from typing import cast

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


def test_an_unexpected_gender_label_is_skipped_with_a_warning_not_raised(
    monkeypatch, db_session, normal_user, caplog
):
    """FIX: speaker_attribute_migration_task.py's gender-accumulation loop.

    A label outside {"male", "female"} used to raise ``KeyError`` on
    ``speaker_probs[sid][gender]``. It is now skipped with a ``logger.warning`` instead —
    proven here by a speaker with ONLY an unexpected-label result: no exception, no
    prediction written (the same as the "no usable audio" branch), but ``attributes_predicted_at``
    is still stamped so the speaker doesn't perpetually show as pending.
    """
    mf = _media_file(db_session, normal_user)
    sp = _speaker(db_session, mf, normal_user, "SPEAKER_00")

    monkeypatch.setattr(sat, "session_scope", lambda: _scope_yielding(db_session))

    results = {
        "gender": [SegmentResult(model_name="gender", speaker_id=sp.id, value=("unknown", 0.5))]
    }

    with caplog.at_level(logging.WARNING, logger=sat.__name__):
        count = sat._gender_result_writer(_prepared(mf, normal_user), results)

    assert count == 0
    assert any(
        "unknown" in record.getMessage() and str(sp.id) in record.getMessage()
        for record in caplog.records
    ), "the unexpected label and speaker id must be named in the warning for debuggability"

    db_session.expire_all()
    refreshed = db_session.get(Speaker, sp.id)
    assert refreshed.predicted_gender is None
    assert refreshed.attribute_confidence == {"gender": 0.0}
    assert refreshed.attributes_predicted_at is not None


def test_a_speaker_with_some_valid_results_and_one_unexpected_label_still_gets_a_correct_majority(
    monkeypatch, db_session, normal_user
):
    """The unexpected-label result must be skipped, not blank out the whole speaker.

    Two real "male" results plus one bogus-label result for the same speaker: the majority
    label and averaged confidence must be computed from the two valid results ONLY — the
    unexpected one contributes nothing to either the numerator or the clip count used for
    averaging.
    """
    mf = _media_file(db_session, normal_user)
    sp = _speaker(db_session, mf, normal_user, "SPEAKER_00")

    results = {
        "gender": [
            SegmentResult(model_name="gender", speaker_id=sp.id, value=("male", 0.9)),
            SegmentResult(model_name="gender", speaker_id=sp.id, value=("nonbinary", 0.99)),
            SegmentResult(model_name="gender", speaker_id=sp.id, value=("male", 0.7)),
        ]
    }

    monkeypatch.setattr(sat, "session_scope", lambda: _scope_yielding(db_session))
    count = sat._gender_result_writer(_prepared(mf, normal_user), results)

    assert count == 1
    db_session.expire_all()
    refreshed = db_session.get(Speaker, sp.id)
    assert refreshed.predicted_gender == "male"
    # Averaged over the 2 VALID clips only — (0.9 + 0.7) / 2 — not divided by 3 and not
    # including the 0.99 from the skipped "nonbinary" result.
    assert refreshed.attribute_confidence == {"gender": 0.8}
    assert refreshed.attributes_predicted_at is not None


def test_the_unexpected_label_no_longer_fails_the_file_in_the_pipeline(
    monkeypatch, db_session, normal_user
):
    """Drives the REAL migration_pipeline.process_batch_pipelined + REAL _gender_result_writer.

    One file with a normal result, one file whose gender results mix a valid label with an
    unexpected one for the SAME speaker. Proves: the batch does not crash, BOTH files succeed,
    and the mixed-result file's speaker still gets a correct prediction from its valid result
    alone. ffmpeg/MinIO/GPU are bypassed entirely by faking the two pipeline seams that touch
    them.
    """
    mf_good = _media_file(db_session, normal_user)
    sp_good = _speaker(db_session, mf_good, normal_user, "SPEAKER_00")
    mf_mixed = _media_file(db_session, normal_user)
    sp_mixed = _speaker(db_session, mf_mixed, normal_user, "SPEAKER_00")

    canned = {
        str(mf_good.uuid): {
            "gender": [
                SegmentResult(model_name="gender", speaker_id=sp_good.id, value=("female", 0.6))
            ]
        },
        str(mf_mixed.uuid): {
            "gender": [
                SegmentResult(model_name="gender", speaker_id=sp_mixed.id, value=("unknown", 0.5)),
                SegmentResult(model_name="gender", speaker_id=sp_mixed.id, value=("male", 0.8)),
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
            (str(mf_mixed.uuid), _prepared(mf_mixed, normal_user)),
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

    assert success == 2
    assert failed == 0
    assert set(successes) == {str(mf_good.uuid), str(mf_mixed.uuid)}
    assert failures == []

    db_session.expire_all()
    good_refreshed = db_session.get(Speaker, sp_good.id)
    mixed_refreshed = db_session.get(Speaker, sp_mixed.id)
    assert good_refreshed.predicted_gender == "female"
    assert mixed_refreshed.predicted_gender == "male", (
        "the valid 'male' result must still be used despite the unexpected label alongside it"
    )
    assert mixed_refreshed.attribute_confidence == {"gender": 0.8}

    still_pending = {f.id for f in sat._get_files_needing_attribute_detection(db_session)}
    assert mf_good.id not in still_pending
    assert mf_mixed.id not in still_pending


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
