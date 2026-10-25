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
  runs the voiceprint step once, and is the run's metering terminus;
* every stage records what it ran in the file's processing provenance (#1229): the
  transcription, the speaker detector (engine, where, execution, merge) and the voiceprint
  step exactly once.

Fakes sit at the thin seams only: the speaker detector (``_build_detector``), the audio
fetch, the Celery publish, websocket notifications and profile matching (the OpenSearch
voiceprint plane). The inline voiceprint step itself runs for real.
Everything else (task rows, file status, segments, speakers) is real database state.
"""

from __future__ import annotations

import uuid as uuid_pkg
from contextlib import contextmanager
from types import SimpleNamespace

import numpy as np
import pytest

from app.core.constants import CeleryQueues
from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.models.media import Task
from app.models.media import TranscriptSegment
from app.services.diarization.types import DiarizeResult
from app.services.diarization.types import DiarizeSegment
from app.services.pipeline_plan import PYANNOTE_AI
from app.services.pipeline_plan import post_stage_engine
from tests.unit._plan_contexts import TRANSCRIPTION
from tests.unit._plan_contexts import make_plan

_SOURCES = ("provider", "local", "pyannote", "asr_config", "off")


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
    # Another provider's speakers (#1226): after local transcription, and beside cloud text.
    assert ("local_gpu", "asr_config", "full") in POST_STAGE_CELLS
    assert ("local_cpu", "asr_config", "full") in POST_STAGE_CELLS
    assert ("cloud_text_word", "asr_config", "lite") in CELLS


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


class _NoProfileMatching:
    """Profile matching is the OpenSearch kNN plane: the seam, with no profiles."""

    def __init__(self, *a, **k):
        pass

    def process_speaker_embeddings_native(self, **kwargs):
        return []


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
    real_postprocess_inline = postprocess._process_native_embeddings
    real_post_stage_inline = embeddings._process_speaker_embeddings_native

    def _counted(real):
        def run(*args, **kwargs):
            seen["inline"].append(args)
            real(*args, **kwargs)

        return run

    monkeypatch.setattr(
        postprocess, "_process_native_embeddings", _counted(real_postprocess_inline)
    )
    monkeypatch.setattr(
        embeddings, "_process_speaker_embeddings_native", _counted(real_post_stage_inline)
    )
    monkeypatch.setattr(embeddings, "session_scope", lambda: _bridged(db_session))
    monkeypatch.setattr(embeddings, "SpeakerMatchingService", _NoProfileMatching)
    monkeypatch.setattr(
        "app.services.speaker_matching_service.SpeakerMatchingService", _NoProfileMatching
    )
    monkeypatch.setattr(
        "app.services.permission_service.PermissionService.get_accessible_profile_ids_for_file",
        lambda *a, **k: [],
    )
    monkeypatch.setattr(embeddings, "_should_use_native_embeddings", lambda result: True)
    for module in (postprocess, notifications):
        for name in ("send_progress_notification", "send_completion_notification"):
            monkeypatch.setattr(module, name, lambda *a, **k: None)
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


def _provenance(db_session, media) -> dict:
    db_session.expire_all()
    document = (
        db_session.query(MediaFile.processing_provenance).filter(MediaFile.id == media.id).scalar()
    )
    assert document is not None, "no stage recorded anything"
    steps: dict = document["steps"]
    return steps


def _assert_speakers_recorded(steps: dict, plan, provider: str) -> None:
    """The speaker-detection entry names the detector that served, how the plan ran it,
    and where: a vendor named as the detector is that vendor's own speakers (``asr``)."""
    diarization = steps["diarization"]
    assert diarization["provider"] == provider
    engine = "asr" if plan.diarization.engine == "asr_config" else plan.diarization.engine
    assert diarization["engine"] == engine
    assert diarization["where"] == plan.diarization.where
    assert diarization["execution"] == plan.diarization.execution
    assert diarization["merge"] == plan.diarization.merge


def _assert_voiceprints_recorded_once(steps: dict, *, inline: bool) -> None:
    """Free vectors are recorded by the stage that matched them; the CPU embedding stage
    (dispatched, not run, here) records its own, so nothing may claim it in advance."""
    if inline:
        assert steps["voiceprints"]["execution"] == "inline_native"
        assert steps["voiceprints"]["where"] == "server"
    else:
        assert "voiceprints" not in steps


def _served_inline(plan) -> str | None:
    """The id the transcription stage reports for speakers it detected itself."""
    execution = plan.diarization.execution
    if execution == "inline_local":
        return "native"
    if execution == "inline_asr":
        return str(plan.transcription.provider)
    if execution == "parallel_cloud":
        return (
            PYANNOTE_AI if plan.diarization.engine == "pyannote_ai" else plan.diarization.provider
        )
    return None


@pytest.mark.parametrize(("transcription", "source", "mode"), CELLS)
def test_every_stage_of_a_run_records_what_it_ran(
    transcription, source, mode, db_session, normal_user, stage_seams, monkeypatch
):
    """The real chain for one run: transcript save -> postprocess -> (post stage). The
    file's provenance names the transcription, the speaker detector and the voiceprint step,
    each once, as the plan ran them."""
    from app.core.config import settings
    from app.tasks.transcription import finalize as finalize_mod
    from app.tasks.transcription.context import TranscriptionContext
    from app.tasks.transcription.postprocess import finalize_transcription

    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", mode)
    monkeypatch.setattr(finalize_mod, "session_scope", lambda: _bridged(db_session))
    monkeypatch.setattr(finalize_mod, "send_progress_notification", lambda *a, **k: None)
    monkeypatch.setattr(finalize_mod, "send_transcript_ready_notification", lambda *a, **k: None)
    monkeypatch.setattr(finalize_mod, "_should_use_native_embeddings", lambda result: True)
    plan = make_plan(transcription, source, deployment_mode=mode)
    media, run = _file_with_run(db_session, normal_user, words=plan.diarization.merge == "word")
    served = _served_inline(plan)
    inline_vectors = plan.diarization.execution == "inline_local"
    stored = _segments(db_session, media)[0]
    transcript = {
        "segments": [
            {
                "start": stored.start_time,
                "end": stored.end_time,
                "text": stored.text,
                "words": stored.words or [],
                **({"speaker": "SPEAKER_00"} if served else {}),
            }
        ],
        "language": "en",
        "asr_provider": plan.transcription.provider,
        "asr_model": plan.transcription.model,
        "diarization_disabled": plan.diarization.engine == "off",
        "diarization_provider": served,
        "diarization_model": "m-1" if served else None,
        "native_speaker_embeddings": {"SPEAKER_00": np.full(256, 0.1)} if inline_vectors else None,
    }
    ctx = TranscriptionContext(
        task_id=run.id,
        file_id=media.id,
        file_uuid=str(media.uuid),
        user_id=media.user_id,
        file_path=media.storage_path,
        file_name="matrix.wav",
        content_type=media.content_type,
    )

    saved = finalize_mod._process_and_save_critical(
        ctx, transcript, {"pipeline_plan": plan.to_context()}
    )
    finalize_transcription.__wrapped__(saved)
    post_stage = plan.diarization.execution.startswith("post_stage")
    if post_stage:
        engine = post_stage_engine(plan.diarization.engine)
        _run_post_stage(monkeypatch, media, run, plan, _FakeDetector(engine, native=True))
        served = _SERVED[engine]
        inline_vectors = engine == "local"

    steps = _provenance(db_session, media)
    assert steps["transcription"]["provider"] == plan.transcription.provider
    assert steps["transcription"]["where"] == plan.transcription.where
    if plan.diarization.engine == "off":
        assert steps["diarization"]["engine"] == "off"
    else:
        assert served is not None
        _assert_speakers_recorded(steps, plan, served)
    _assert_voiceprints_recorded_once(steps, inline=plan.speaker_id.runs and inline_vectors)
    assert len(stage_seams["inline"]) + len(stage_seams["embedding_stage"]) == (
        1 if plan.speaker_id.runs else 0
    )


# --------------------------------------------------------------------------------------
# 3. the post stage: detection, re-split, voiceprint step, terminus
# --------------------------------------------------------------------------------------


#: The id each post-stage engine reports for itself: this server's engine, the pyannote.ai
#: service, and the saved vendor named as the detector (``SPEAKER_CONFIG``: Gladia).
_SERVED = {"local": "native", "pyannote_ai": PYANNOTE_AI, "asr_config": "gladia"}


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
                    {seg.speaker: np.full(256, 0.1) for seg in segments} if self.native else None
                ),
            }
        return DiarizeResult(
            segments=segments,
            num_speakers=len(segments),
            provider_name=_SERVED[self.engine],
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
    assert media.diarization_provider == _SERVED[engine]
    steps = _provenance(db_session, media)
    _assert_speakers_recorded(steps, plan, _SERVED[engine])
    _assert_voiceprints_recorded_once(steps, inline=inline_vectors)


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


# --------------------------------------------------------------------------------------
# 4. another provider's speakers (#1226): the REAL adapter behind the post stage
# --------------------------------------------------------------------------------------


def test_local_whisper_with_another_providers_speakers_runs_end_to_end(
    db_session, normal_user, stage_seams, monkeypatch
):
    """Local transcription + ``asr_config``: postprocess hands off to the post stage on
    ``cloud-asr``; the post stage builds the REAL ``ASRDiarizationAdapter`` through
    ``create_for_engine`` from a saved Gladia config, Gladia is stubbed at its HTTP seam,
    the turns are merged with ``assign_speakers`` and the segment is re-split. Only the
    vendor's turns survive: its text is never stored."""
    from app.models import UserSetting
    from app.models.user_asr_settings import UserASRSettings
    from app.tasks import rediarize_task as rdz
    from app.tasks.transcription.postprocess import finalize_transcription
    from app.utils.encryption import encrypt_api_key
    from tests.helpers import stub_public_dns
    from tests.unit.test_gladia_provider import _BASE
    from tests.unit.test_gladia_provider import _dispatch_post
    from tests.unit.test_gladia_provider import _FakeResponse
    from tests.unit.test_gladia_provider import _mock_session

    cfg = UserASRSettings(
        user_id=normal_user.id,
        name=f"speakers-{uuid_pkg.uuid4().hex[:8]}",
        provider="gladia",
        model_name="standard",
        api_key=encrypt_api_key("gladia-key"),
    )
    db_session.add(cfg)
    db_session.add(
        UserSetting(
            user_id=normal_user.id,
            setting_key="transcription_diarization_source",
            setting_value="asr_config",
        )
    )
    db_session.commit()
    from app.services.pipeline_plan import DiarizerConfigInfo

    plan = make_plan(
        "local_gpu",
        "asr_config",
        diarization_config=DiarizerConfigInfo(
            provider="gladia", model="standard", uuid=str(cfg.uuid), returns_speakers=True
        ),
    )
    assert plan.diarization.execution == "post_stage_cloud"
    assert plan.diarization.config_uuid == str(cfg.uuid)
    media, run = _file_with_run(db_session, normal_user)

    finalize_transcription.__wrapped__(_gpu_result(media, run, plan))
    assert len(stage_seams["post_stage"]) == 1
    sent = stage_seams["post_stage"][0]
    assert sent["queue"] == CeleryQueues.CLOUD_ASR
    assert sent["kwargs"]["engine"] == "asr_config"

    stub_public_dns(monkeypatch, domain="api.gladia.io")
    utterances = [
        {
            "text": "vendor-only-words alpha",
            "start": 0.0,
            "end": 2.0,
            "speaker": 0,
            "words": [
                {"word": "vendor-only-words", "start": 0.0, "end": 1.0},
                {"word": "alpha", "start": 1.0, "end": 2.0},
            ],
        },
        {
            "text": "vendor-only-words beta",
            "start": 2.0,
            "end": 4.0,
            "speaker": 1,
            "words": [
                {"word": "vendor-only-words", "start": 2.0, "end": 3.0},
                {"word": "beta", "start": 3.0, "end": 4.0},
            ],
        },
    ]
    done = _FakeResponse(
        json_data={"status": "done", "result": {"transcription": {"utterances": utterances}}}
    )
    captured: dict = {}
    _mock_session(
        monkeypatch,
        post_side_effect=_dispatch_post(
            _FakeResponse(json_data={"audio_url": "https://cdn.gladia.io/a"}),
            _FakeResponse(json_data={"result_url": f"{_BASE}/v2/transcription/j"}),
            captured,
        ),
        get_side_effect=lambda *a, **k: done,
    )
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(rdz, "_prepare_audio", lambda *a, **k: (__file__, None))

    result = rdz.rediarize_task.apply(kwargs={**sent["kwargs"], "file_uuid": str(media.uuid)}).get()

    assert result["status"] == "success", result
    assert captured["body"]["diarization"] is True
    segments = _segments(db_session, media)
    assert [(s.start_time, s.end_time) for s in segments] == [(0.0, 2.0), (2.0, 4.0)]
    assert [s.text.strip() for s in segments] == ["hello there", "good friend"]
    assert segments[0].speaker_id != segments[1].speaker_id
    assert not any("vendor-only-words" in s.text for s in segments)
    db_session.refresh(media)
    assert media.status == FileStatus.COMPLETED
    assert media.diarization_provider == "gladia"
    # One voiceprint step (vendor speakers carry no vectors) and one metering terminus.
    assert len(stage_seams["embedding_stage"]) == 1
    assert [ctx.run_id for ctx in stage_seams["meter"]] == [run.id]
    # Provenance: Gladia's own speakers, in the cloud, merged per word, after the fact.
    steps = _provenance(db_session, media)
    _assert_speakers_recorded(steps, plan, "gladia")
    assert (steps["diarization"]["engine"], steps["diarization"]["where"]) == ("asr", "cloud")
    assert steps["diarization"]["model"] == "standard"
    _assert_voiceprints_recorded_once(steps, inline=False)
