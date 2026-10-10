"""A file's per-file request is stored on dispatch and replayed on a re-run (issue #1203).

Dispatch runs for real against real rows; only the broker publish is stubbed, and the
assertions are on the keyword arguments of the first pipeline stage, i.e. what the pipeline
is actually built with.
"""

from __future__ import annotations

import contextlib
import uuid
from types import SimpleNamespace

import pytest

import app.tasks.transcription.dispatch as dispatch_module
from app.models.media import MediaFile
from app.tasks.transcription.requested_options import resolve_dispatch_options


@pytest.fixture
def stages(monkeypatch, db_session):
    @contextlib.contextmanager
    def _scope():
        yield db_session

    captured: list[dict] = []

    def _chain(preprocess, *rest):
        captured.append(dict(preprocess.kwargs))
        return SimpleNamespace(apply_async=lambda **kw: SimpleNamespace(id="stub"))

    monkeypatch.setattr(dispatch_module, "session_scope", _scope)
    monkeypatch.setattr(dispatch_module, "chain", _chain)
    return captured


@pytest.fixture
def make_file(db_session, normal_user):
    def _make(**overrides) -> MediaFile:
        file_uuid = str(uuid.uuid4())
        row = MediaFile(
            uuid=file_uuid,
            filename="req.wav",
            storage_path=f"media/test/{file_uuid}.wav",
            content_type="audio/wav",
            file_size=1,
            status="error",
            user_id=normal_user.id,
            **overrides,
        )
        db_session.add(row)
        db_session.commit()
        return row

    return _make


def test_a_re_run_replays_model_range_and_skipped_diarization(stages, make_file):
    row = make_file(
        requested_whisper_model="base",
        requested_min_speakers=2,
        requested_max_speakers=4,
        requested_num_speakers=3,
    )
    dispatch_module.dispatch_transcription_pipeline(str(row.uuid), reuse_requested_options=True)
    stage = stages[0]
    assert stage["whisper_model"] == "base"
    assert (stage["min_speakers"], stage["max_speakers"], stage["num_speakers"]) == (2, 4, 3)


def test_a_re_run_of_a_normal_file_leaves_the_diarization_source_to_the_user(stages, make_file):
    row = make_file(requested_whisper_model="large-v3-turbo", requested_min_speakers=2)
    dispatch_module.dispatch_transcription_pipeline(str(row.uuid), reuse_requested_options=True)
    stage = stages[0]
    assert stage["diarization_source"] is None
    assert stage["disable_diarization"] is None
    assert stage["whisper_model"] == "large-v3-turbo"


def test_an_explicit_argument_beats_the_stored_one(stages, make_file):
    row = make_file(requested_min_speakers=2, requested_max_speakers=4)
    dispatch_module.dispatch_transcription_pipeline(
        str(row.uuid), min_speakers=3, reuse_requested_options=True
    )
    assert (stages[0]["min_speakers"], stages[0]["max_speakers"]) == (3, 4)


def test_a_fresh_request_replaces_the_stored_one_even_with_none(stages, make_file, db_session):
    """A reprocess that asks for the defaults must not leave an old "tiny" for the next retry."""
    row = make_file(
        requested_whisper_model="tiny", requested_min_speakers=2, requested_disable_diarization=True
    )
    dispatch_module.dispatch_transcription_pipeline(str(row.uuid))
    stage = stages[0]
    assert stage["whisper_model"] is None
    assert stage["min_speakers"] is None
    assert stage["diarization_source"] is None
    db_session.refresh(row)
    assert row.requested_whisper_model is None
    assert row.requested_min_speakers is None
    assert row.requested_disable_diarization is None


def test_a_fresh_request_is_stored_for_the_next_re_run(stages, make_file, db_session):
    row = make_file()
    dispatch_module.dispatch_transcription_pipeline(
        str(row.uuid), min_speakers=2, max_speakers=6, disable_diarization=True
    )
    db_session.refresh(row)
    assert (row.requested_min_speakers, row.requested_max_speakers) == (2, 6)
    assert row.requested_disable_diarization is True

    dispatch_module.dispatch_transcription_pipeline(str(row.uuid), reuse_requested_options=True)
    assert stages[1]["diarization_source"] == "off"
    assert (stages[1]["min_speakers"], stages[1]["max_speakers"]) == (2, 6)


def test_a_model_stored_before_the_deployment_locked_model_choice_is_dropped(
    stages, make_file, monkeypatch
):
    row = make_file(requested_whisper_model="tiny")
    monkeypatch.setattr("app.core.locked_settings.capability_enabled", lambda key, request: False)
    dispatch_module.dispatch_transcription_pipeline(str(row.uuid), reuse_requested_options=True)
    assert stages[0]["whisper_model"] is None


def test_resolution_keeps_a_stored_skip_as_off_not_provider():
    row = MediaFile(
        uuid=str(uuid.uuid4()),
        filename="x.wav",
        storage_path="x",
        content_type="audio/wav",
        file_size=1,
        requested_disable_diarization=True,
    )
    got = resolve_dispatch_options(
        row,
        reuse_requested_options=True,
        whisper_model=None,
        min_speakers=None,
        max_speakers=None,
        num_speakers=None,
        disable_diarization=None,
        diarization_source=None,
    )
    assert got.diarization_source == "off"
    assert got.disable_diarization is None


def test_the_recovery_service_retry_the_spa_uses_replays_the_files_request(
    stages, make_file, db_session, monkeypatch
):
    """``/my-files/{uuid}/retry`` (the route the SPA calls) reaches dispatch only through
    ``schedule_file_retry``; it must replay the file's model and range, not defaults."""
    from app.services.task_recovery_service import task_recovery_service

    row = make_file(requested_whisper_model="base", requested_min_speakers=2)
    monkeypatch.setattr(task_recovery_service, "_session_scope", lambda: _yield_session(db_session))

    assert task_recovery_service.schedule_file_retry(row.id) is True
    assert stages[0]["whisper_model"] == "base"
    assert stages[0]["min_speakers"] == 2


@contextlib.contextmanager
def _yield_session(db):
    yield db
