"""Every executable cell of the pipeline plan runs the way the plan says (issue #1225).

``tests/unit/test_pipeline_plan.py`` pins WHAT the resolver decides for each combination of
transcription engine, speaker-detection source and deployment. This file pins that the
stages DO it, for every cell the plan calls executable:

* dispatch builds the transcription link the plan names (task and queue);
* postprocess hands speaker detection to the post stage exactly when the plan's execution is
  ``post_stage_*`` (with the plan's engine, merge and queue), otherwise runs the voiceprint
  step once (inline, or the CPU embedding stage), and meters the run only when nothing was
  handed off: one terminus per run;
* the post stage (``rediarize_task``) re-splits the stored transcript at speaker changes,
  runs the voiceprint step once, and is the run's metering terminus.

Fakes sit at the thin seams only: the speaker detector (``_build_detector``), the audio
fetch, the Celery publish, websocket notifications and the OpenSearch voiceprint write.
Everything else (task rows, file status, segments, speakers) is real database state.
"""

from __future__ import annotations

import uuid as uuid_pkg
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.core.constants import CeleryQueues
from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.models.media import Task
from app.models.media import TranscriptSegment
from app.services.diarization.types import DiarizeResult
from app.services.diarization.types import DiarizeSegment
from app.services.pipeline_plan import post_stage_engine
from tests.unit._plan_contexts import TRANSCRIPTION
from tests.unit._plan_contexts import make_plan

_SOURCES = ("provider", "local", "pyannote", "off")


def _executable_cells() -> list[tuple[str, str, str]]:
    cells = []
    for mode in ("full", "lite"):
        for transcription in TRANSCRIPTION:
            for source in _SOURCES:
                if not make_plan(transcription, source, deployment_mode=mode).blocking():
                    cells.append((transcription, source, mode))
    return cells


CELLS = _executable_cells()
POST_STAGE_CELLS = [
    cell
    for cell in CELLS
    if make_plan(cell[0], cell[1], deployment_mode=cell[2]).diarization.execution.startswith(
        "post_stage"
    )
]


def test_the_matrix_is_not_vacuous():
    """Guard the guard: the parametrization must cover every execution the plan has."""
    executions = {make_plan(t, s, deployment_mode=m).diarization.execution for t, s, m in CELLS}
    assert executions == {
        "inline_asr",
        "inline_local",
        "parallel_cloud",
        "post_stage_local",
        "post_stage_cloud",
        "none",
    }
    assert ("local_gpu", "pyannote", "full") in POST_STAGE_CELLS
    assert ("local_cpu", "provider", "full") in POST_STAGE_CELLS
    assert ("cloud_text_segment", "provider", "full") in POST_STAGE_CELLS


# --------------------------------------------------------------------------------------
# 1. dispatch: the transcription link
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("transcription", "source", "mode"), CELLS)
def test_dispatch_builds_the_transcription_link_the_plan_names(transcription, source, mode):
    from app.tasks.transcription.dispatch import _transcribe_stage

    plan = make_plan(transcription, source, deployment_mode=mode)
    link = _transcribe_stage(plan, cpu_priority=5, gpu_priority=5)

    expected_task = (
        "transcription.cpu_transcribe"
        if transcription == "local_cpu"
        else "transcription.gpu_transcribe"
    )
    assert link.task == expected_task
    assert link.options["queue"] == plan.transcription.queue


def test_local_whisper_with_pyannote_transcribes_on_the_gpu_and_detects_on_cloud_asr():
    plan = make_plan("local_gpu", "pyannote")
    assert plan.transcription.queue == CeleryQueues.GPU
    assert plan.diarization.execution == "post_stage_cloud"
    assert plan.diarization.queue == CeleryQueues.CLOUD_ASR
    assert post_stage_engine(plan.diarization.engine) == "pyannote_ai"


def test_gpu_split_never_splits_a_run_whose_speakers_come_from_a_post_stage():
    """The split topology's diarize leg is inline detection; a post stage replaces it."""
    split_inline = make_plan("local_gpu", "provider", gpu_split=True)
    split_post = make_plan("local_gpu", "pyannote", gpu_split=True)
    assert split_inline.transcription.queue == CeleryQueues.GPU_TRANSCRIBE
    assert split_post.transcription.queue == CeleryQueues.GPU


# --------------------------------------------------------------------------------------
# 2. postprocess: hand-off, voiceprint step, metering terminus
# --------------------------------------------------------------------------------------


@contextmanager
def _bridged(db_session):
    yield db_session
    db_session.commit()


@pytest.fixture
def stage_seams(db_session, monkeypatch):
    """The out-of-process seams of postprocess and the post stage, recorded."""
    from app.tasks import rediarize_task as rdz
    from app.tasks import speaker_embedding_task
    from app.tasks.transcription import embeddings
    from app.tasks.transcription import hooks
    from app.tasks.transcription import notifications
    from app.tasks.transcription import postprocess

    seen: dict[str, list] = {"post_stage": [], "embedding_stage": [], "inline": [], "meter": []}
    monkeypatch.setattr(hooks, "_transcription_complete_hooks", [seen["meter"].append])
    monkeypatch.setattr(postprocess, "session_scope", lambda: _bridged(db_session))
    monkeypatch.setattr(rdz, "session_scope", lambda: _bridged(db_session))
    monkeypatch.setattr(
        rdz.rediarize_task,
        "apply_async",
        lambda *a, **k: seen["post_stage"].append(k),
    )
    monkeypatch.setattr(
        speaker_embedding_task.extract_speaker_embeddings_task,
        "apply_async",
        lambda *a, **k: seen["embedding_stage"].append(k),
    )
    monkeypatch.setattr(
        postprocess, "_process_native_embeddings", lambda *a, **k: seen["inline"].append(a)
    )
    monkeypatch.setattr(
        embeddings, "_process_speaker_embeddings_native", lambda *a, **k: seen["inline"].append(a)
    )
    monkeypatch.setattr(embeddings, "_should_use_native_embeddings", lambda result: True)
    for module in (postprocess, notifications):
        for name in ("send_progress_notification", "send_completion_notification"):
            monkeypatch.setattr(module, name, lambda *a, **k: None)
    monkeypatch.setattr(postprocess, "send_ws_event_for_file", lambda *a, **k: None)
    monkeypatch.setattr(postprocess, "_persist_timing_row", lambda *a, **k: None)
    monkeypatch.setattr(postprocess, "_schedule_timing_tail_flush", lambda *a, **k: None)
    monkeypatch.setattr(postprocess, "_cleanup_temp", lambda *a, **k: None)
    monkeypatch.setattr(postprocess.enrich_and_dispatch, "delay", lambda *a, **k: None)
    monkeypatch.setattr(rdz, "_prepare_audio", lambda *a, **k: ("/scratch/a.wav", None))
    monkeypatch.setattr(rdz, "cleanup_temp_audio", lambda file_uuid: None)
    monkeypatch.setattr(rdz, "_after_speakers_changed", lambda *a, **k: None)
    return seen


def _file_with_run(db_session, user, *, words: bool = True) -> tuple[MediaFile, Task]:
    """A transcribed file (one word-timed segment) and its pipeline run's task row."""
    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename="matrix.wav",
        storage_path=f"user_{user.id}/matrix.wav",
        file_size=1,
        content_type="audio/wav",
        status=FileStatus.PROCESSING,
    )
    db_session.add(media)
    db_session.commit()
    # Whisper's word tokens carry their leading space; segment text is their concatenation.
    word_list = [
        {"word": " hello", "start": 0.0, "end": 1.0},
        {"word": " there", "start": 1.0, "end": 2.0},
        {"word": " good", "start": 2.0, "end": 3.0},
        {"word": " friend", "start": 3.0, "end": 4.0},
    ]
    db_session.add(
        TranscriptSegment(
            uuid=uuid_pkg.uuid4(),
            media_file_id=media.id,
            start_time=0.0,
            end_time=4.0,
            text="hello there good friend",
            words=word_list if words else None,
        )
    )
    run = Task(
        id=f"run-{uuid_pkg.uuid4().hex[:12]}",
        user_id=user.id,
        media_file_id=media.id,
        task_type="transcription",
        status="in_progress",
    )
    db_session.add(run)
    db_session.commit()
    return media, run


def _gpu_result(media: MediaFile, run: Task, plan) -> dict:
    inline = plan.speaker_id.execution == "inline_native"
    return {
        "status": "success",
        "file_uuid": str(media.uuid),
        "file_id": media.id,
        "user_id": media.user_id,
        "task_id": run.id,
        "speaker_mapping": {"SPEAKER_00": 1} if plan.diarization.engine != "off" else {},
        "native_embeddings": {"SPEAKER_00": [0.1] * 256} if inline else None,
        "use_native_embeddings": inline,
        "asr_provider": plan.transcription.provider,
        "downstream_tasks": None,
        "pipeline_plan": plan.to_context(),
    }


@pytest.mark.parametrize(("transcription", "source", "mode"), CELLS)
def test_postprocess_follows_the_plan(
    transcription, source, mode, db_session, normal_user, stage_seams
):
    from app.tasks.transcription.postprocess import finalize_transcription

    plan = make_plan(transcription, source, deployment_mode=mode)
    media, run = _file_with_run(db_session, normal_user)

    result = finalize_transcription.__wrapped__(_gpu_result(media, run, plan))

    assert result["status"] == "success"
    execution = plan.diarization.execution
    if execution.startswith("post_stage"):
        assert len(stage_seams["post_stage"]) == 1
        sent = stage_seams["post_stage"][0]
        assert sent["queue"] == plan.diarization.queue
        assert sent["kwargs"]["engine"] == post_stage_engine(plan.diarization.engine)
        assert sent["kwargs"]["merge"] == plan.diarization.merge
        assert sent["kwargs"]["pipeline_completion"] is True
        assert sent["kwargs"]["pipeline_task_id"] == run.id
        # The voiceprint step and the metering terminus belong to the post stage.
        assert stage_seams["embedding_stage"] == []
        assert stage_seams["inline"] == []
        assert stage_seams["meter"] == []
    else:
        assert stage_seams["post_stage"] == []
        voiceprint_runs = len(stage_seams["embedding_stage"]) + len(stage_seams["inline"])
        assert voiceprint_runs == (1 if plan.speaker_id.runs else 0)
        if plan.speaker_id.execution == "embedding_stage":
            assert stage_seams["embedding_stage"][0]["queue"] == CeleryQueues.CPU
            # The embedding stage completes and meters the run.
            assert stage_seams["meter"] == []
        else:
            assert len(stage_seams["meter"]) == 1
            assert stage_seams["meter"][0].run_id == run.id
    db_session.refresh(run)
    expected_status = "in_progress" if stage_seams["embedding_stage"] else "completed"
    assert run.status == expected_status


# --------------------------------------------------------------------------------------
# 3. the post stage: detection, re-split, voiceprint step, terminus
# --------------------------------------------------------------------------------------


class _FakeDetector:
    """Two turns over the stored segment: SPEAKER_00 for 0-2 s, SPEAKER_01 for 2-4 s."""

    def __init__(self, engine: str, *, native: bool, turns=((0.0, 2.0), (2.0, 4.0))):
        self.engine, self.native, self.turns, self.calls = engine, native, turns, 0

    def diarize(self, audio_path, config, progress_callback=None):
        from app.transcription.speaker_assigner import diarize_segments_to_frame

        self.calls += 1
        segments = [
            DiarizeSegment(start=start, end=end, speaker=f"SPEAKER_{i:02d}")
            for i, (start, end) in enumerate(self.turns)
        ]
        metadata = {}
        if self.engine == "local":
            metadata = {
                "diarize_df": diarize_segments_to_frame(segments),
                "overlap_info": {},
                "native_embeddings": (
                    {seg.speaker: [0.1] * 256 for seg in segments} if self.native else None
                ),
            }
        return DiarizeResult(
            segments=segments,
            num_speakers=len(segments),
            provider_name="native" if self.engine == "local" else "pyannote",
            model_name="community-1" if self.engine == "local" else "precision-3",
            metadata=metadata,
        )


def _run_post_stage(monkeypatch, media, run, plan, detector) -> dict:
    from app.tasks import rediarize_task as rdz

    monkeypatch.setattr(rdz, "_build_detector", lambda *a, **k: detector)
    result: dict = rdz.rediarize_task.apply(
        kwargs={
            "file_uuid": str(media.uuid),
            "pipeline_completion": True,
            "pipeline_task_id": run.id,
            "engine": post_stage_engine(plan.diarization.engine),
            "merge": plan.diarization.merge,
        }
    ).get()
    return result


def _segments(db_session, media) -> list[TranscriptSegment]:
    db_session.expire_all()
    segments: list[TranscriptSegment] = (
        db_session.query(TranscriptSegment)
        .filter(TranscriptSegment.media_file_id == media.id)
        .order_by(TranscriptSegment.start_time)
        .all()
    )
    return segments


@pytest.mark.parametrize(("transcription", "source", "mode"), POST_STAGE_CELLS)
@pytest.mark.parametrize("native", [True, False])
def test_the_post_stage_completes_the_run_once(
    transcription, source, mode, native, db_session, normal_user, stage_seams, monkeypatch
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", mode)
    plan = make_plan(transcription, source, deployment_mode=mode)
    engine = post_stage_engine(plan.diarization.engine)
    media, run = _file_with_run(db_session, normal_user, words=plan.diarization.merge == "word")
    detector = _FakeDetector(engine, native=native)

    result = _run_post_stage(monkeypatch, media, run, plan, detector)

    assert result["status"] == "success", result
    assert detector.calls == 1
    # Exactly one voiceprint step: free with this server's vectors, else the CPU stage.
    inline_vectors = engine == "local" and native
    assert len(stage_seams["inline"]) == (1 if inline_vectors else 0)
    assert len(stage_seams["embedding_stage"]) == (0 if inline_vectors else 1)
    if stage_seams["embedding_stage"]:
        sent = stage_seams["embedding_stage"][0]
        assert sent["queue"] == CeleryQueues.CPU
        assert sent["kwargs"] == {"pipeline_task_id": run.id, "closes_pipeline": False}
    # The post stage is the run's ONE metering terminus, under the pipeline's id.
    assert [ctx.run_id for ctx in stage_seams["meter"]] == [run.id]
    db_session.refresh(media)
    assert media.status == FileStatus.COMPLETED
    assert media.diarization_provider == ("native" if engine == "local" else "pyannote")


def test_a_word_timed_post_stage_resplits_a_segment_at_the_speaker_change(
    db_session, normal_user, stage_seams, monkeypatch
):
    """The fix to the re-run path: rediarize used to save speakers without
    ``finalize_segments``, leaving one segment spanning both speakers."""
    plan = make_plan("local_gpu", "pyannote")
    media, run = _file_with_run(db_session, normal_user)

    _run_post_stage(monkeypatch, media, run, plan, _FakeDetector("pyannote_ai", native=False))

    segments = _segments(db_session, media)
    assert [(s.start_time, s.end_time) for s in segments] == [(0.0, 2.0), (2.0, 4.0)]
    assert [s.text.strip() for s in segments] == ["hello there", "good friend"]
    assert segments[0].speaker_id != segments[1].speaker_id


def test_a_segment_timed_post_stage_gives_the_segment_its_dominant_speaker(
    db_session, normal_user, stage_seams, monkeypatch
):
    """Segment timing (``segment_timing_only``): no split inside a segment, the turn with
    the most overlap names it."""
    plan = make_plan("cloud_text_segment", "provider")
    assert plan.diarization.merge == "segment"
    media, run = _file_with_run(db_session, normal_user, words=False)
    detector = _FakeDetector("local", native=False, turns=((0.0, 3.0), (3.0, 4.0)))

    _run_post_stage(monkeypatch, media, run, plan, detector)

    segments = _segments(db_session, media)
    assert len(segments) == 1
    assert (segments[0].start_time, segments[0].end_time) == (0.0, 4.0)
    from app.models.media import Speaker

    speaker = db_session.query(Speaker).filter(Speaker.id == segments[0].speaker_id).one()
    assert speaker.name == "SPEAKER_00"


def test_a_cloud_detector_failure_keeps_the_transcript_and_records_why(
    db_session, normal_user, stage_seams, monkeypatch
):
    from app.tasks.rediarize_task import CLOUD_DIARIZATION_FAILED_MESSAGE

    class _VendorDown:
        def diarize(self, *a, **k):
            raise RuntimeError("upstream 503 from vendor host internal-7.example")

    plan = make_plan("local_gpu", "pyannote")
    media, run = _file_with_run(db_session, normal_user)

    result = _run_post_stage(monkeypatch, media, run, plan, _VendorDown())

    assert result["status"] == "completed_without_speakers"
    db_session.refresh(media)
    assert media.status == FileStatus.COMPLETED
    segments = _segments(db_session, media)
    assert [s.text for s in segments] == ["hello there good friend"]
    post_stage_row = (
        db_session.query(Task)
        .filter(Task.media_file_id == media.id, Task.task_type == "rediarize")
        .one()
    )
    assert post_stage_row.status == "completed"
    assert post_stage_row.error_message == CLOUD_DIARIZATION_FAILED_MESSAGE
    assert "vendor" not in (post_stage_row.error_message or "").lower()
    # Still the run's terminus: the transcription happened and is metered once.
    assert [ctx.run_id for ctx in stage_seams["meter"]] == [run.id]
    assert stage_seams["embedding_stage"] == []


def test_lite_refuses_a_local_post_stage(db_session, normal_user, stage_seams, monkeypatch):
    """Defence in depth: lite has no local speaker detection, whatever reached the task."""
    from app.core.config import settings
    from app.services.pipeline_plan import ISSUE_MESSAGES

    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "lite")
    media, run = _file_with_run(db_session, normal_user)
    detector = _FakeDetector("local", native=False)

    result = _run_post_stage(
        monkeypatch,
        media,
        run,
        SimpleNamespace(diarization=SimpleNamespace(engine="local", merge="word")),
        detector,
    )

    assert result["status"] == "error"
    assert result["message"] == ISSUE_MESSAGES["local_diarization_unavailable"]
    assert detector.calls == 0
    assert stage_seams["meter"] == []
