"""Metering pipeline coverage — proves the LIVE transcription chain reaches the
transcription-complete hook with a populated ``CompletionContext``.

This is the gap that hid the metering bug: ``fire_transcription_complete`` was
only wired into dead legacy code (``core._run_post_gpu_background``), never into
the live 3-stage chain terminus (``postprocess.finalize_transcription``) or the
cloud-ASR rediarize completion (``rediarize_task``).

Two layers:

* ``test_finalize_fires_completion_hook_standalone`` — runs with NO external
  services (pure in-memory hook registration + a fake MediaFile). Asserts the
  metering helper builds a ``CompletionContext`` with the right org_id, file_id,
  duration, run_id, provider and success. This is the fast, always-green proof
  that the completion path reaches the hook with populated fields.

* ``test_finalize_chain_writes_usage_event`` — ``@pytest.mark.integration``;
  needs the live dev stack (Postgres). Seeds a real finalized ``MediaFile``,
  registers a hook that writes a real ``usage_event`` row via the core usage
  spine, runs ``finalize_transcription`` EAGERLY, and asserts the row landed
  with the run_id as the idempotency scope. Excluded by the default
  ``-m 'not integration'`` selector; runs in the local integration gate.

Run the standalone test anywhere:
    cd backend && PYTHONPATH=. pytest tests/integration/test_metering_pipeline.py -v
Run the full chain against the live stack:
    cd backend && PYTHONPATH=. pytest -m integration \
        tests/integration/test_metering_pipeline.py -v
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from tests.helpers import does_not_raise


@pytest.fixture(autouse=True)
def _clear_hooks():
    """Ensure no hooks leak between tests (registry is module-global)."""
    from app.tasks.transcription.hooks import clear_hooks

    clear_hooks()
    yield
    clear_hooks()


class _FakeMediaFile:
    """Minimal stand-in for a finalized MediaFile row."""

    def __init__(self, file_id, org_id, duration, user_id=7):
        self.id = file_id
        self.uuid = uuidlib.uuid4()
        self.user_id = user_id
        self.organization_id = org_id
        self.duration = duration


class _FakeSession:
    """DB stub whose query(...).filter(...).first() returns a fixed MediaFile."""

    def __init__(self, media_file):
        self._media_file = media_file

    def query(self, *_args, **_kwargs):
        return self

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._media_file


def test_finalize_fires_completion_hook_standalone():
    """The live completion helper reaches the hook with a populated context.

    No DB / Redis / broker — registers an in-memory recording hook and drives
    ``_fire_completion_metering`` directly (the exact helper the live
    ``finalize_transcription`` terminus calls).
    """
    from app.tasks.transcription.hooks import register_transcription_complete
    from app.tasks.transcription.postprocess import _fire_completion_metering

    captured = []
    register_transcription_complete(lambda ctx: captured.append(ctx))

    media_file = _FakeMediaFile(file_id=4242, org_id=99, duration=123.5, user_id=7)
    db = _FakeSession(media_file)

    run_id = "stable-run-id-abc123"
    _fire_completion_metering(db, file_id=4242, run_id=run_id, provider="local", success=True)

    assert len(captured) == 1, "completion hook was not invoked by the live helper"
    ctx = captured[0]
    assert ctx.file_id == 4242
    assert ctx.organization_id == 99
    assert ctx.audio_duration_s == pytest.approx(123.5)
    assert ctx.run_id == run_id  # stable pipeline task id == idempotency scope
    assert ctx.provider == "local"
    assert ctx.success is True
    assert ctx.user_id == 7
    assert ctx.file_uuid == str(media_file.uuid)


def test_metering_hook_failure_never_propagates():
    """A throwing metering hook must not break a finished transcription."""
    from app.tasks.transcription.hooks import register_transcription_complete
    from app.tasks.transcription.postprocess import _fire_completion_metering

    def _boom(_ctx):
        raise RuntimeError("metering backend down")

    register_transcription_complete(_boom)

    db = _FakeSession(_FakeMediaFile(file_id=1, org_id=None, duration=None))
    # Must return cleanly despite the hook raising.
    with does_not_raise("a failing metering hook must never surface to the caller"):
        _fire_completion_metering(db, file_id=1, run_id="r1", provider="local", success=True)


def test_community_edition_is_noop():
    """With no hook registered (community default), firing is a harmless no-op."""
    from app.tasks.transcription.postprocess import _fire_completion_metering

    db = _FakeSession(_FakeMediaFile(file_id=1, org_id=None, duration=10.0))
    with does_not_raise("the community edition has no metering hook, so dispatch is a no-op"):
        _fire_completion_metering(db, file_id=1, run_id="r1", provider="local", success=True)


@pytest.mark.integration
def test_finalize_chain_writes_usage_event():
    """Live eager chain: finalize_transcription -> completion hook -> usage_event row.

    Seeds a real finalized MediaFile, registers a hook that writes a usage_event
    via the core usage spine, runs the real ``finalize_transcription`` task
    EAGERLY, and asserts exactly one usage_event row was written keyed on the
    pipeline run_id. This is the regression guard the metering gap lacked.
    """
    from app.core.enums import FileStatus
    from app.db.session_utils import session_scope
    from app.models.media import MediaFile
    from app.models.usage_event import UsageEvent
    from app.models.user import User
    from app.services.usage_service import record_event
    from app.tasks.transcription.hooks import register_transcription_complete
    from app.tasks.transcription.postprocess import finalize_transcription

    run_id = f"meter-test-{uuidlib.uuid4().hex[:12]}"

    # Hook mirrors what the cloud layer registers: one usage_event per completed run.
    def _meter(ctx):
        with session_scope() as hook_db:
            record_event(
                hook_db,
                event_type="transcription.hours",
                quantity=(ctx.audio_duration_s or 0) / 3600.0,
                unit="hours",
                user_id=ctx.user_id,
                organization_id=ctx.organization_id,
                file_id=ctx.file_id,
                idempotency_key=f"{ctx.file_id}:{ctx.run_id}",
                metadata={"provider": ctx.provider},
            )

    register_transcription_complete(_meter)

    created_file_id = None
    try:
        with session_scope() as db:
            user = db.query(User).first()
            if user is None:
                user = User(
                    email=f"meter-test-{uuidlib.uuid4().hex[:8]}@example.com",
                    hashed_password="x",
                    is_active=True,
                )
                db.add(user)
                db.flush()
            mf = MediaFile(
                uuid=uuidlib.uuid4(),
                user_id=user.id,
                filename="meter_fixture.wav",
                storage_path="test/meter_fixture.wav",
                file_size=1,
                content_type="audio/wav",
                language="en",
                duration=600.0,  # 10 minutes -> 0.1667 hours
                status=FileStatus.COMPLETED,
            )
            db.add(mf)
            db.flush()
            created_file_id = int(mf.id)
            created_uuid = str(mf.uuid)
            created_user_id = int(user.id)

        # Drive the real terminus eagerly. The "else" (local ASR / diarization
        # disabled) branch is the single-task completion path that meters.
        gpu_result = {
            "status": "success",
            "file_uuid": created_uuid,
            "file_id": created_file_id,
            "user_id": created_user_id,
            "task_id": run_id,
            "speaker_mapping": {},
            "native_embeddings": None,
            "use_native_embeddings": False,
            "asr_provider": "local",
            "diarization_disabled": True,  # forces the local single-task terminus
            "downstream_tasks": [],
        }

        finalize_transcription.apply(args=[gpu_result]).get()

        with session_scope() as db:
            rows = (
                db.query(UsageEvent)
                .filter(UsageEvent.idempotency_key == f"{created_file_id}:{run_id}")
                .all()
            )
            # Assert while the rows are still bound to the session (reading an
            # attribute on a detached UsageEvent would raise DetachedInstanceError).
            assert len(rows) == 1, "live finalize chain did not write exactly one usage_event"
            row = rows[0]
            assert row.file_id == created_file_id
            assert row.event_type == "transcription.hours"
            # NUMERIC column quantizes to 3 decimals (0.16667 -> 0.167), so use abs tol.
            assert float(row.quantity) == pytest.approx(600.0 / 3600.0, abs=1e-3)
    finally:
        # Clean up the seeded file + its usage_event (dev data is sacred).
        if created_file_id is not None:
            with session_scope() as db:
                db.query(UsageEvent).filter(
                    UsageEvent.idempotency_key == f"{created_file_id}:{run_id}"
                ).delete()
                mf = db.query(MediaFile).filter(MediaFile.id == created_file_id).first()
                if mf:
                    db.delete(mf)
