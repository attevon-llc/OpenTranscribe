"""The speaker-attribute task must not hold a DB transaction across inference.

`detect_speaker_attributes` (CPU queue) used to wrap its whole body in one
`session_scope()`: it SELECTed `transcript_segment`, then loaded a wav2vec2
model and ran ffmpeg segment fetches over a presigned URL *inside* that
transaction. On 2026-08-13 one run stalled in the fetch phase for three hours
(until Celery's hard time limit) and left the CPU worker's Postgres backend
`idle in transaction` for the whole time, last statement the
`transcript_segment` SELECT. That transaction holds ACCESS SHARE on
`transcript_segment`, so `ALTER TABLE` — an Alembic upgrade — queues behind it,
it pins the vacuum horizon on the largest table in the product, and it burns a
pool connection.

These tests instrument the module's `session_scope` and assert, from inside the
slow phase itself, that no scope is open at that moment. They fail against the
single-scope version of the task.
"""

import contextlib

import pytest

from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker
from app.tasks import speaker_attribute_task as sat
from tests.fixtures.speaker_attribute_tracking import add_file_with_speakers


class _ScopeTracker:
    """Stands in for `session_scope`, recording how many scopes are open."""

    def __init__(self, session):
        self._session = session
        self.depth = 0
        self.max_depth = 0
        self.opened = 0
        #: Depth observed at each point the slow phase reported in.
        self.depth_during_slow_phase: list[tuple[str, int]] = []

    @contextlib.contextmanager
    def scope(self):
        self.depth += 1
        self.opened += 1
        self.max_depth = max(self.max_depth, self.depth)
        try:
            yield self._session
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        finally:
            self.depth -= 1

    def observe(self, label: str) -> None:
        self.depth_during_slow_phase.append((label, self.depth))


@pytest.fixture
def tracked(db_session, monkeypatch):
    """Patch the task module so the slow phase is observable and instant."""
    tracker = _ScopeTracker(db_session)
    monkeypatch.setattr(sat, "session_scope", tracker.scope)
    monkeypatch.setattr(sat, "_is_speaker_attribute_detection_enabled", lambda user_id: True)
    monkeypatch.setattr(sat, "_dispatch_llm_speaker_identification", lambda file_uuid: None)
    monkeypatch.setattr(sat, "send_ws_event_for_file", lambda *a, **kw: None)
    # send_ws_event_for_file (issue #908) resolves quarantine via its OWN
    # session_scope() on a real connection, which cannot see this test's
    # savepoint-isolated MediaFile — stub the check itself rather than bridge
    # a second session, keeping this suite focused on scope depth.
    monkeypatch.setattr(
        "app.services.file_availability.is_notification_suppressed_for_uuid",
        lambda *a, **kw: False,
    )

    class _FakeMinio:
        def presigned_get_object(self, **kwargs):
            tracker.observe("presigned_get_object")
            return "http://minio.invalid/attrs.mp4"

    monkeypatch.setattr("app.services.minio_service.minio_client", _FakeMinio())

    class _FakeService:
        def load_models(self):
            # Real implementation loads a wav2vec2 checkpoint (seconds to
            # minutes, network on a cold cache).
            tracker.observe("load_models")

    monkeypatch.setattr(
        "app.services.speaker_attribute_service.get_cached_attribute_service",
        lambda: _FakeService(),
    )
    return tracker


def test_no_db_scope_open_during_model_load_and_inference(
    db_session, normal_user, tracked, monkeypatch
):
    """The leak regression: the slow phase must run with zero scopes open."""
    media_file, speakers = add_file_with_speakers(
        db_session, normal_user, speakers=2, seg_seconds=4.0
    )

    # The task builds `work_items` from an UNORDERED `db.query(Speaker.id)`, so which
    # speaker lands at index 0 is whatever Postgres returns first — not necessarily
    # `speakers[0]`. Record the id actually scored and assert against THAT row, rather
    # than assuming the two coincide. They usually do, which is why this test passed
    # for a long time and then failed once in a full parallel run with
    # `predicted_gender == None`: the fixture's other speaker had been scored instead.
    scored: dict[str, int] = {}

    def fake_inference(audio_source, work_items, service, audio_stream_index=None):
        tracked.observe("gender_inference")
        assert work_items, "fixture produced no work items — the test would prove nothing"
        scored["speaker_id"] = work_items[0][0]
        return (
            {work_items[0][0]: {"male": 0.9, "female": 0.1}},
            {work_items[0][0]: 1},
        )

    monkeypatch.setattr(sat, "_run_gender_inference_parallel", fake_inference)

    result = sat._detect_speaker_attributes(str(media_file.uuid), normal_user.id, "test-task-id")

    assert result["status"] == "success", result

    observed = dict(tracked.depth_during_slow_phase)
    assert observed == {
        "presigned_get_object": 0,
        "load_models": 0,
        "gender_inference": 0,
    }, f"a DB transaction was held across the slow phase: {tracked.depth_during_slow_phase}"

    # The read phase must actually have opened a session — otherwise "0 open
    # during inference" would be true of a task that never touched the DB.
    assert tracked.opened >= 2, f"expected a read scope and a write scope, got {tracked.opened}"
    assert tracked.max_depth == 1, "session scopes must not nest"

    # And the results still land in the DB — on the speaker that was actually scored.
    assert scored, "inference never ran, so nothing below would be testing a write"
    written = next(s for s in speakers if s.id == scored["speaker_id"])
    db_session.refresh(written)
    assert written.predicted_gender == "male"
    assert written.attribute_confidence == {"gender": 0.9}
    assert result["speakers_updated"] == 1

    # The speaker that was NOT scored must be left alone — otherwise the assertions
    # above would also pass if the task wrote the same verdict to every speaker.
    other = next(s for s in speakers if s.id != scored["speaker_id"])
    db_session.refresh(other)
    assert other.predicted_gender is None


def test_scope_is_released_when_inference_fails(db_session, normal_user, tracked, monkeypatch):
    """A failure in the slow phase must not leave a scope open either."""
    media_file, _ = add_file_with_speakers(db_session, normal_user, speakers=2, seg_seconds=4.0)

    def boom(audio_source, work_items, service, audio_stream_index=None):
        tracked.observe("gender_inference")
        raise RuntimeError("ffmpeg segment fetch failed")

    monkeypatch.setattr(sat, "_run_gender_inference_parallel", boom)

    result = sat._detect_speaker_attributes(str(media_file.uuid), normal_user.id, "test-task-id")

    assert result["status"] == "error"
    assert dict(tracked.depth_during_slow_phase)["gender_inference"] == 0
    assert tracked.depth == 0, "a session scope survived the failure"


def test_read_phase_returns_plain_data(db_session, normal_user, tracked):
    """`_load_detection_inputs` must not hand back live ORM instances.

    Returning ORM objects would push lazy loads — and therefore a new
    transaction — into the slow phase, reintroducing the leak by the back door.
    """
    media_file, speakers = add_file_with_speakers(
        db_session, normal_user, speakers=1, seg_seconds=4.0
    )

    inputs = sat._load_detection_inputs(str(media_file.uuid))

    assert tracked.depth == 0
    assert inputs is not None
    assert inputs["file_id"] == media_file.id
    assert inputs["speaker_ids"] == [speakers[0].id]
    assert inputs["segment_count"] == 2
    assert inputs["speaker_segments"] == {
        speakers[0].id: [{"start": 0.0, "end": 4.0}, {"start": 4.0, "end": 8.0}]
    }
    for value in inputs.values():
        assert not isinstance(value, (MediaFile, Speaker, TranscriptSegment))
