"""One precedence for the speaker range: per-file > the user's saved range > env (issue #1198).

Each path asserts the number that reaches the diarizer or the re-diarize task, against real
``UserSetting`` rows rather than a mocked settings dict. The user's saved range is 3/5 in every
test and the deployment env default is 1/20, so a path that loses the user's range shows up as
1 or 20 -- which is what the watch-source, cloud + local, and re-diarize paths used to produce.
"""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock

import pytest

from app.core.config import settings
from app.models.prompt import UserSetting
from app.schemas.watch_source import WatchSourceCreate
from app.services.asr.types import ASRConfig
from app.tasks import rediarize_task as rediarize_module
from app.tasks.transcription import cloud_asr
from app.tasks.transcription import pipelines
from app.tasks.transcription import postprocess
from app.tasks.transcription.user_settings import SpeakerRange
from app.tasks.transcription.user_settings import resolve_speaker_range
from app.tasks.transcription.user_settings import resolve_speaker_range_for_user
from app.transcription.config import TranscriptionConfig
from app.transcription.engine.config import EngineConfig


@contextlib.contextmanager
def _scope_of(db):
    yield db


@pytest.fixture
def saved_range(db_session, normal_user, monkeypatch):
    """The user's saved 3/5 range, visible to every ``session_scope`` the code under test opens."""
    for key, value in (("transcription_min_speakers", "3"), ("transcription_max_speakers", "5")):
        db_session.add(UserSetting(user_id=normal_user.id, setting_key=key, setting_value=value))
    db_session.commit()
    monkeypatch.setattr(settings, "MIN_SPEAKERS", 1)
    monkeypatch.setattr(settings, "MAX_SPEAKERS", 20)
    monkeypatch.setattr(settings, "NUM_SPEAKERS", None)
    monkeypatch.setattr("app.db.session_utils.session_scope", lambda: _scope_of(db_session))
    for module in (pipelines, cloud_asr, postprocess):
        monkeypatch.setattr(module, "session_scope", lambda: _scope_of(db_session))
    return normal_user


# -- the helper itself -------------------------------------------------------------------------


def test_per_file_beats_saved_beats_env(saved_range):
    assert resolve_speaker_range_for_user(saved_range.id, None, None, None) == SpeakerRange(
        3, 5, None
    )
    assert resolve_speaker_range_for_user(saved_range.id, 2, None, 4) == SpeakerRange(2, 5, 4)
    assert resolve_speaker_range_for_user(saved_range.id, 2, 8, 4) == SpeakerRange(2, 8, 4)


def test_env_default_applies_only_when_the_user_saved_nothing(db_session, normal_user, monkeypatch):
    monkeypatch.setattr(settings, "MIN_SPEAKERS", 2)
    monkeypatch.setattr(settings, "MAX_SPEAKERS", 9)
    monkeypatch.setattr(settings, "NUM_SPEAKERS", 4)
    monkeypatch.setattr("app.db.session_utils.session_scope", lambda: _scope_of(db_session))
    assert resolve_speaker_range_for_user(normal_user.id, None, None, None) == SpeakerRange(2, 9, 4)


def test_resolver_takes_an_already_loaded_settings_dict(monkeypatch):
    monkeypatch.setattr(settings, "NUM_SPEAKERS", 6)
    got = resolve_speaker_range({"min_speakers": 3, "max_speakers": 5}, None, 7, None)
    assert got == SpeakerRange(3, 7, 6)


# -- local paths -------------------------------------------------------------------------------


class _StopError(Exception):
    pass


def _capture_overrides(monkeypatch) -> list[dict]:
    """Stop each local builder at the point it hands its overrides to a config factory."""
    seen: list[dict] = []

    def _record(cls, **overrides):
        seen.append(overrides)
        raise _StopError

    monkeypatch.setattr(TranscriptionConfig, "from_environment", classmethod(_record))
    monkeypatch.setattr(EngineConfig, "from_environment", classmethod(_record))
    monkeypatch.setattr(pipelines, "update_task_status", lambda *a, **k: None)
    monkeypatch.setattr(pipelines, "send_progress_notification", lambda *a, **k: None)
    monkeypatch.setattr(pipelines, "_resolve_language_settings", lambda c, s, t: ("en", False))
    monkeypatch.setattr(pipelines, "_load_file_vocabulary", lambda ctx: None)
    return seen


@pytest.mark.parametrize("builder", ["monolithic", "engine", "transcribe_only"])
@pytest.mark.parametrize(
    ("per_file", "expected"),
    [((None, None, None), (3, 5)), ((2, None, None), (2, 5)), ((None, 9, None), (3, 9))],
    ids=["none", "min-only", "max-only"],
)
def test_local_paths_resolve_the_range_per_file_then_saved(
    saved_range, monkeypatch, builder, per_file, expected
):
    seen = _capture_overrides(monkeypatch)
    ctx = MagicMock(task_id="t", file_id=1, user_id=saved_range.id)
    with pytest.raises(_StopError):
        if builder == "monolithic":
            pipelines._run_transcription_pipeline(ctx, "/a.wav", *per_file)
        else:
            run = (
                pipelines._run_engine_pipeline
                if builder == "engine"
                else pipelines._run_transcribe_only_stage
            )
            context = dict(
                zip(("min_speakers", "max_speakers", "num_speakers"), per_file, strict=True)
            )
            run(ctx, "/a.wav", context)
    assert (seen[0]["min_speakers"], seen[0]["max_speakers"]) == expected


# -- cloud ASR ---------------------------------------------------------------------------------


class _CapturingProvider:
    provider_name = "deepgram"

    def __init__(self):
        self.config: ASRConfig | None = None

    def supports_translation(self):
        return True

    def supports_diarization(self):
        return True

    def transcribe(self, audio_path, config, progress_callback):
        self.config = config
        raise _StopError


def test_cloud_asr_hands_the_saved_range_to_the_provider(saved_range, monkeypatch):
    monkeypatch.setattr(cloud_asr, "send_progress_notification", lambda *a, **k: None)
    monkeypatch.setattr(cloud_asr, "update_task_status", lambda *a, **k: None)
    provider = _CapturingProvider()
    ctx = MagicMock(task_id="t", file_id=1, user_id=saved_range.id)
    with pytest.raises(_StopError):
        cloud_asr._run_cloud_asr_pipeline(ctx, "/a.wav", None, None, None, provider=provider)
    config = provider.config
    assert config is not None
    assert (config.min_speakers, config.max_speakers) == (3, 5)


# -- cloud ASR + local diarization -------------------------------------------------------------


def _local_diarization_result(user_id, **extra):
    return {
        "file_uuid": "uuid-1198",
        "file_id": 11,
        "user_id": user_id,
        "task_id": "task-1198",
        "speaker_mapping": {},
        "asr_provider": "deepgram",
        "diarization_source": "local",
        "diarization_disabled": False,
        "downstream_tasks": None,
        **extra,
    }


@pytest.fixture
def finalize_seams(monkeypatch):
    for name in (
        "send_progress_notification",
        "send_completion_notification",
        "update_task_status",
        "send_ws_event_for_file",
        "_cleanup_temp",
        "_persist_timing_row",
        "_schedule_timing_tail_flush",
    ):
        monkeypatch.setattr(postprocess, name, lambda *a, **k: None)
    monkeypatch.setattr(postprocess, "update_task_status_with_duration", lambda *a, **k: (None, 0))
    monkeypatch.setattr(postprocess, "enrich_and_dispatch", MagicMock())
    monkeypatch.setattr(postprocess, "_fire_completion_metering", lambda *a, **k: None)
    calls: list[dict] = []
    monkeypatch.setattr(
        rediarize_module.rediarize_task,
        "apply_async",
        lambda *a, **k: calls.append(k["kwargs"]),
    )
    return calls


def test_cloud_plus_local_rediarize_carries_the_saved_range(saved_range, finalize_seams):
    postprocess.finalize_transcription.__wrapped__(_local_diarization_result(saved_range.id))
    assert len(finalize_seams) == 1
    kwargs = finalize_seams[0]
    assert (kwargs["min_speakers"], kwargs["max_speakers"]) == (3, 5)


def test_cloud_plus_local_rediarize_prefers_the_files_own_range(saved_range, finalize_seams):
    postprocess.finalize_transcription.__wrapped__(
        _local_diarization_result(saved_range.id, min_speakers=2, num_speakers=4)
    )
    kwargs = finalize_seams[0]
    assert (kwargs["min_speakers"], kwargs["max_speakers"], kwargs["num_speakers"]) == (2, 5, 4)


# -- re-diarize --------------------------------------------------------------------------------


def test_rediarize_resolves_a_blank_range_to_the_saved_one(saved_range, db_session, monkeypatch):
    """The stage a user re-runs from the reprocess dialog with the range fields left blank."""
    import uuid

    from app.models.media import MediaFile

    media = MediaFile(
        uuid=str(uuid.uuid4()),
        filename="re.wav",
        storage_path="media/test/re.wav",
        content_type="audio/wav",
        file_size=1,
        status="completed",
        user_id=saved_range.id,
    )
    db_session.add(media)
    db_session.commit()
    captured: list[SpeakerRange] = []

    def _diarize(audio_file_path, speaker_range, wav_path=None):
        captured.append(speaker_range)
        raise _StopError

    monkeypatch.setattr(rediarize_module, "session_scope", lambda: _scope_of(db_session))
    monkeypatch.setattr(rediarize_module, "_prepare_audio", lambda *a, **k: ("/a.wav", None))
    monkeypatch.setattr(
        rediarize_module, "_load_segments_as_transcript", lambda fid: {"segments": []}
    )
    monkeypatch.setattr(rediarize_module, "_run_diarization", _diarize)
    monkeypatch.setattr(
        "app.tasks.transcription.notifications.send_progress_notification", lambda *a, **k: None
    )

    for per_file, expected in (
        ((None, None, None), SpeakerRange(3, 5, None)),
        ((2, None, None), SpeakerRange(2, 5, None)),
    ):
        captured.clear()
        rediarize_module.rediarize_task.apply(
            kwargs={
                "file_uuid": str(media.uuid),
                "min_speakers": per_file[0],
                "max_speakers": per_file[1],
                "num_speakers": per_file[2],
            }
        )
        assert captured == [expected]


# -- watch sources -----------------------------------------------------------------------------


def test_a_new_watch_source_has_no_range_of_its_own():
    source = WatchSourceCreate(name="w", source_type="local", local_path="sub")
    assert source.min_speakers is None
    assert source.max_speakers is None


def test_a_watch_import_passes_no_range_so_the_saved_one_applies(monkeypatch):
    from app.services.watch_sources import processing

    seen: dict = {}

    def _tail(media_file, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr("app.api.endpoints.files.upload.dispatch_upload_pipeline", _tail)
    source = MagicMock(min_speakers=None, max_speakers=None)
    assert processing._dispatch_pipeline(MagicMock(), 1, source) is None
    assert seen["min_speakers"] is None
    assert seen["max_speakers"] is None
