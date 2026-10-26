"""Lite is cloud-only for transcription and speaker detection: every local option is a 409.

Issue #1228 (plan E10): on ``DEPLOYMENT_MODE=lite`` there is no local Whisper and no local
speaker detection, so every API surface that would select one answers 409 with the plan's
``{"code", "message"}`` (a stable code and its fixed sentence, never exception text) and
changes nothing. Each row below runs twice: on lite it must be refused with its code and
leave the stored state as it was; on the full image (the control) the same request must
succeed, so a handler that refused everything, or keyed on something other than the
deployment, fails here by name.

The rows cover the user settings PUT, the four ASR switches (create, update, set-active,
clear-active, delete), the super-admin local model and engine endpoints, and the per-file
Whisper model on upload/reprocess.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from fixtures.hf_cache import make_complete_repo

from app.models import UserSetting
from app.models.user_asr_settings import UserASRSettings
from app.services.pipeline_plan import ISSUE_MESSAGES
from tests.helpers import detail_dict

_ASR = "/api/asr-settings"
_TRANSCRIPTION = "/api/user-settings/transcription"
_ENGINE = "/api/admin/engine-settings"

LOCAL_ASR = "local_asr_unavailable"
LOCAL_DIAR = "local_diarization_unavailable"
AUTO_LITE = "automatic_needs_speaker_provider"

# The engine endpoints write SystemSettings rows another module also writes (issue #389).
pytestmark = [
    pytest.mark.usefixtures("pipeline_env"),
    pytest.mark.xdist_group("engine_system_settings"),
]


@dataclass(frozen=True)
class World:
    """Everything a row needs: the client, the caller's headers, and state helpers."""

    client: TestClient
    headers: dict
    admin_headers: dict
    add_asr_config: Callable[..., UserASRSettings]
    store: Callable[[str, str], None]
    stored: Callable[[str], str | None]


@pytest.fixture
def world(
    client,
    user_token_headers,
    super_admin_token_headers,
    normal_user,
    db_session,
    add_asr_config,
    hf_cache,
) -> World:
    # The full-image control activates a local model, and activating one now requires it to be
    # on disk (#1246): provide both candidates `_set_local_model` picks between.
    make_complete_repo(hf_cache, "Systran/faster-whisper-large-v3")
    make_complete_repo(hf_cache, "Systran/faster-whisper-large-v2")

    def store(key: str, value: str) -> None:
        db_session.add(UserSetting(user_id=normal_user.id, setting_key=key, setting_value=value))
        db_session.commit()

    def stored(key: str) -> str | None:
        db_session.expire_all()
        row = (
            db_session.query(UserSetting)
            .filter(UserSetting.user_id == normal_user.id, UserSetting.setting_key == key)
            .one_or_none()
        )
        return None if row is None else str(row.setting_value)

    return World(
        client=client,
        headers=user_token_headers,
        admin_headers=super_admin_token_headers,
        add_asr_config=add_asr_config,
        store=store,
        stored=stored,
    )


# --- the rows -------------------------------------------------------------------------
# Each row: arrange + act, returning (response, a snapshot of the state it must not change
# on refusal, a callable re-reading that state).


def _put_source(w: World, value: str):
    def snap() -> str | None:
        return w.stored("transcription_diarization_source")

    before = snap()
    response = w.client.put(_TRANSCRIPTION, json={"diarization_source": value}, headers=w.headers)
    return response, before, snap


def _put_source_local(w: World):
    w.add_asr_config("deepgram", "nova-3", active=True)
    return _put_source(w, "local")


def _put_source_local_on_local_whisper(w: World):
    """No cloud config: transcription is refused too, and the source must still say why."""
    return _put_source(w, "local")


def _put_automatic_text_only(w: World):
    w.store("transcription_diarization_source", "pyannote")
    w.add_asr_config("openai", "whisper-1", active=True)
    return _put_source(w, "provider")


def _active_pointer(w: World):
    def snap() -> str | None:
        return w.stored("active_asr_config_id")

    return snap


def _set_active_local(w: World):
    current = w.add_asr_config("deepgram", "nova-3", active=True)
    local = w.add_asr_config("local", "large-v3-turbo")
    body = {"config_uuid": str(local.uuid)}
    response = w.client.post(f"{_ASR}/set-active", json=body, headers=w.headers)
    return response, str(current.id), _active_pointer(w)


def _clear_active(w: World):
    current = w.add_asr_config("deepgram", "nova-3", active=True)
    response = w.client.post(f"{_ASR}/clear-active", headers=w.headers)
    return response, str(current.id), _active_pointer(w)


def _create_local(w: World):
    def snap() -> int:
        return len(w.client.get(_ASR, headers=w.headers).json().get("configs", []))

    before = snap()
    body = {"name": "local-created", "provider": "local", "model_name": "large-v3-turbo"}
    return w.client.post(_ASR, json=body, headers=w.headers), before, snap


def _update_into_local(w: World):
    cfg = w.add_asr_config("deepgram", "nova-3", active=True)

    def snap() -> str:
        return str(w.client.get(f"{_ASR}/config/{cfg.uuid}", headers=w.headers).json()["provider"])

    return (
        w.client.put(
            f"{_ASR}/config/{cfg.uuid}",
            json={"provider": "local", "model_name": "large-v3-turbo"},
            headers=w.headers,
        ),
        "deepgram",
        snap,
    )


def _delete_the_only_active_config(w: World):
    cfg = w.add_asr_config("deepgram", "nova-3", active=True)

    def snap() -> int:
        return int(w.client.get(f"{_ASR}/config/{cfg.uuid}", headers=w.headers).status_code)

    return w.client.delete(f"{_ASR}/config/{cfg.uuid}", headers=w.headers), 200, snap


def _set_local_model(w: World):
    def snap() -> str:
        response = w.client.get(f"{_ASR}/local-model/active", headers=w.admin_headers)
        return str(response.json()["active_model"])

    before = snap()
    target = "large-v3" if before != "large-v3" else "large-v2"
    return (
        w.client.post(
            f"{_ASR}/local-model/set", json={"model_name": target}, headers=w.admin_headers
        ),
        before,
        snap,
    )


def _engine(field: str, value: object):
    def row(w: World):
        def snap():
            return w.client.get(_ENGINE, headers=w.admin_headers).json()[field]

        before = snap()
        return (
            w.client.post(f"{_ENGINE}/update", json={field: value}, headers=w.admin_headers),
            before,
            snap,
        )

    row.__name__ = f"engine_{field}"
    return row


#: (row, lite code, full-image status)
ROWS = [
    (_put_source_local, LOCAL_DIAR, status.HTTP_200_OK),
    (_put_source_local_on_local_whisper, LOCAL_DIAR, status.HTTP_200_OK),
    (_put_automatic_text_only, AUTO_LITE, status.HTTP_200_OK),
    (_set_active_local, LOCAL_ASR, status.HTTP_200_OK),
    (_clear_active, LOCAL_ASR, status.HTTP_200_OK),
    (_create_local, LOCAL_ASR, status.HTTP_201_CREATED),
    (_update_into_local, LOCAL_ASR, status.HTTP_200_OK),
    (_delete_the_only_active_config, LOCAL_ASR, status.HTTP_200_OK),
    (_set_local_model, LOCAL_ASR, status.HTTP_200_OK),
    (_engine("diarizer_backend", "pyannote"), LOCAL_DIAR, status.HTTP_200_OK),
    (_engine("diarizer_require_sidecar", True), LOCAL_DIAR, status.HTTP_200_OK),
    (_engine("transcriber_backend", "whisperx"), LOCAL_ASR, status.HTTP_200_OK),
]


@pytest.mark.parametrize(("row", "code", "_full"), ROWS, ids=[r[0].__name__ for r in ROWS])
def test_lite_refuses_every_local_option_and_changes_nothing(world, pipeline_env, row, code, _full):
    pipeline_env("lite")
    response, before, snapshot = row(world)

    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert response.json()["detail"] == {"code": code, "message": ISSUE_MESSAGES[code]}
    assert snapshot() == before


@pytest.mark.parametrize(("row", "_code", "full"), ROWS, ids=[r[0].__name__ for r in ROWS])
def test_the_full_image_accepts_the_same_request(world, row, _code, full):
    """Control: the deployment, not the request, is what each lite refusal keys on."""
    response, _before, _snapshot = row(world)

    assert response.status_code == full, response.text


# --- what stays allowed on lite ---------------------------------------------------------


def test_lite_keeps_turning_speakers_off_and_boundary_smoothing(world, pipeline_env):
    """What lite still allows: a choice that runs nothing locally, and the smoother."""
    pipeline_env("lite")
    world.add_asr_config("openai", "whisper-1", active=True)

    off = world.client.put(
        _TRANSCRIPTION, json={"diarization_source": "off"}, headers=world.headers
    )
    assert off.status_code == status.HTTP_200_OK, off.text

    smoothing = world.client.post(
        f"{_ENGINE}/update", json={"boundary_smoothing_enabled": True}, headers=world.admin_headers
    )
    assert smoothing.status_code == status.HTTP_200_OK, smoothing.text


def test_lite_delete_keeps_a_cloud_config_active_instead_of_falling_back_to_local(
    world, pipeline_env
):
    """Deleting the active config promotes another cloud config; nothing is refused."""
    pipeline_env("lite")
    active = world.add_asr_config("deepgram", "nova-3", active=True)
    other = world.add_asr_config("assemblyai", "universal")

    response = world.client.delete(f"{_ASR}/config/{active.uuid}", headers=world.headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["config_uuid"] == str(other.uuid)
    assert world.stored("active_asr_config_id") == str(other.id)
    assert LOCAL_ASR not in {i["code"] for i in response.json()["pipeline_issues"]}


def test_lite_never_offers_this_server_as_a_speaker_detector(
    client, user_token_headers, pipeline_env
):
    url = f"{_TRANSCRIPTION}/system-defaults"
    full = client.get(url, headers=user_token_headers)
    assert full.status_code == status.HTTP_200_OK
    assert "local" in full.json()["valid_diarization_sources"]

    pipeline_env("lite")
    lite = client.get(url, headers=user_token_headers)
    assert lite.status_code == status.HTTP_200_OK
    sources = lite.json()["valid_diarization_sources"]
    assert "local" not in sources
    assert {"provider", "pyannote", "off"} <= set(sources)


def test_lite_restart_refuses_without_signalling_any_worker(
    client, super_admin_token_headers, pipeline_env, monkeypatch
):
    """On lite the restart used to fall through to a shutdown BROADCAST to every worker."""
    from app.core.celery import celery_app

    calls: list[str] = []

    class _Control:
        def inspect(self, timeout=None):
            calls.append("inspect")
            raise AssertionError("inspected the workers")

        def broadcast(self, *args, **kwargs):
            calls.append("broadcast")

    monkeypatch.setattr(celery_app, "control", _Control())
    pipeline_env("lite")

    response = client.post(f"{_ASR}/local-model/restart", headers=super_admin_token_headers)

    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert response.json()["detail"]["code"] == LOCAL_ASR
    assert calls == []


def test_full_restart_reaches_the_worker_control(client, super_admin_token_headers, monkeypatch):
    """Control for the lite refusal: on the full image the same request reaches celery."""
    from app.core.celery import celery_app

    calls: list[str] = []

    class _Inspector:
        def ping(self):
            calls.append("ping")
            return {"gpu-transcription@w1": {"ok": "pong"}}

        def active(self):
            return {}

    class _Control:
        def inspect(self, timeout=None):
            return _Inspector()

        def broadcast(self, *args, **kwargs):
            calls.append("broadcast")

    monkeypatch.setattr(celery_app, "control", _Control())

    response = client.post(f"{_ASR}/local-model/restart", headers=super_admin_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert calls == ["ping", "broadcast"]


# --- the per-file Whisper model -----------------------------------------------------------


@pytest.mark.parametrize("model", ["tiny", "base.en"])
def test_lite_refuses_a_per_file_whisper_model(pipeline_env, model):
    """The one chokepoint upload (prepare/complete/URL) and reprocess call (A7)."""
    from fastapi import HTTPException

    from app.utils.whisper_model_choice import require_servable_whisper_model

    assert require_servable_whisper_model(model) == model  # full image: the CPU route

    pipeline_env("lite")
    with pytest.raises(HTTPException) as refused:
        require_servable_whisper_model(model)
    assert refused.value.status_code == status.HTTP_409_CONFLICT
    assert detail_dict(refused.value) == {"code": LOCAL_ASR, "message": ISSUE_MESSAGES[LOCAL_ASR]}
    assert require_servable_whisper_model(None) is None  # no model: the cloud provider


# --- re-runs: refused before anything is cleared -------------------------------------------


@pytest.fixture
def transcribed_file(db_session, normal_user, monkeypatch):
    """A completed file with one speaker; its media counts as present in storage."""
    import uuid as uuid_pkg

    from app.models.media import FileStatus
    from app.models.media import MediaFile
    from app.models.media import Speaker
    from app.utils import task_utils

    monkeypatch.setattr(task_utils, "transcript_is_regenerable", lambda db, media_file: True)
    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=normal_user.id,
        filename="rerun.wav",
        storage_path=f"user_{normal_user.id}/rerun.wav",
        file_size=1,
        content_type="audio/wav",
        status=FileStatus.COMPLETED,
    )
    db_session.add(media)
    db_session.commit()
    speaker = Speaker(
        uuid=uuid_pkg.uuid4(),
        user_id=normal_user.id,
        media_file_id=media.id,
        name="SPEAKER_00",
    )
    db_session.add(speaker)
    db_session.commit()
    return media


def _speaker_count(db_session, media) -> int:
    from app.models.media import Speaker

    db_session.expire_all()
    return int(db_session.query(Speaker).filter(Speaker.media_file_id == media.id).count())


def _rediarize(client, headers, media):
    return client.post(
        f"/api/files/{media.uuid}/reprocess", json={"stages": ["rediarize"]}, headers=headers
    )


@pytest.mark.parametrize("source", ["local", "provider"])
def test_lite_refuses_re_detection_on_this_server_before_clearing_speakers(
    client, user_token_headers, db_session, transcribed_file, world, pipeline_env, source
):
    """Automatic cannot re-run a vendor's speakers after the fact, so it means this server
    (plan Q9): on lite both are refused, and the speakers are still there."""
    pipeline_env("lite")
    world.add_asr_config("deepgram", "nova-3", active=True)
    world.store("transcription_diarization_source", source)

    response = _rediarize(client, user_token_headers, transcribed_file)

    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert response.json()["detail"] == {"code": LOCAL_DIAR, "message": ISSUE_MESSAGES[LOCAL_DIAR]}
    assert _speaker_count(db_session, transcribed_file) == 1


def test_lite_re_detection_with_pyannote_ai_goes_ahead(
    client,
    user_token_headers,
    db_session,
    transcribed_file,
    world,
    pipeline_env,
    store_pyannote_key,
):
    """Control: a cloud detector re-runs on lite (its speakers are cleared for the re-run)."""
    pipeline_env("lite")
    world.add_asr_config("deepgram", "nova-3", active=True)
    world.store("transcription_diarization_source", "pyannote")
    store_pyannote_key()

    response = _rediarize(client, user_token_headers, transcribed_file)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert _speaker_count(db_session, transcribed_file) == 0


def test_the_full_image_re_detects_on_this_server(
    client, user_token_headers, db_session, transcribed_file, world
):
    """Control for the lite refusal: same request, full image."""
    world.add_asr_config("deepgram", "nova-3", active=True)
    world.store("transcription_diarization_source", "local")

    response = _rediarize(client, user_token_headers, transcribed_file)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert _speaker_count(db_session, transcribed_file) == 0


def test_lite_refuses_a_transcription_re_run_before_deleting_the_transcript(
    client, user_token_headers, db_session, transcribed_file, world, pipeline_env
):
    """A full re-run deletes the transcript first; a plan lite cannot run now 409s first."""
    pipeline_env("lite")
    world.add_asr_config("deepgram", "nova-3", active=True)
    world.store("transcription_diarization_source", "local")

    response = client.post(
        f"/api/files/{transcribed_file.uuid}/reprocess", json={}, headers=user_token_headers
    )

    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert response.json()["detail"]["code"] == LOCAL_DIAR
    assert _speaker_count(db_session, transcribed_file) == 1


@pytest.mark.parametrize("deployment", ["lite", "full"])
def test_bulk_re_detection_reports_the_lite_refusal_per_file_before_clearing_speakers(
    client,
    user_token_headers,
    db_session,
    transcribed_file,
    world,
    pipeline_env,
    monkeypatch,
    deployment,
):
    """The gallery's bulk action makes the single-file refusal per file: on lite the result
    carries the plan's code and fixed sentence and the speakers stay; the full image re-runs."""
    from app.api.endpoints.files import management

    monkeypatch.setattr(management, "transcript_is_regenerable", lambda db, media_file: True)
    if deployment == "lite":
        pipeline_env("lite")
    world.add_asr_config("deepgram", "nova-3", active=True)
    world.store("transcription_diarization_source", "local")

    response = client.post(
        "/api/files/management/bulk-action",
        json={
            "file_uuids": [str(transcribed_file.uuid)],
            "action": "reprocess",
            "stages": ["rediarize"],
        },
        headers=user_token_headers,
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    (result,) = response.json()
    if deployment == "lite":
        assert (result["success"], result["error"], result["message"]) == (
            False,
            LOCAL_DIAR,
            ISSUE_MESSAGES[LOCAL_DIAR],
        )
        assert _speaker_count(db_session, transcribed_file) == 1
    else:
        assert (result["success"], result["error"]) == (True, None)
        assert _speaker_count(db_session, transcribed_file) == 0


def test_lite_manual_re_detection_with_pyannote_ai_is_published_on_cloud_asr(
    db_session, normal_user, world, pipeline_env, store_pyannote_key, monkeypatch
):
    """The post stage a lite re-detection publishes: pyannote.ai on cloud-asr, never cpu."""
    from contextlib import contextmanager

    from app.api.endpoints.files import reprocess
    from app.core.constants import CeleryQueues
    from app.db import session_utils
    from app.tasks.rediarize_task import rediarize_task

    @contextmanager
    def _bridged():
        yield db_session

    pipeline_env("lite")
    world.add_asr_config("deepgram", "nova-3", active=True)
    world.store("transcription_diarization_source", "pyannote")
    store_pyannote_key()
    sent: list[dict] = []
    monkeypatch.setattr(rediarize_task, "apply_async", lambda *a, **k: sent.append(k))
    monkeypatch.setattr(session_utils, "session_scope", _bridged)

    reprocess._dispatch_manual_rediarize(
        "11111111-2222-3333-4444-555555555555",
        normal_user.id,
        min_speakers=None,
        max_speakers=None,
        num_speakers=None,
        downstream_tasks=None,
    )

    assert len(sent) == 1
    assert sent[0]["queue"] == CeleryQueues.CLOUD_ASR
    assert sent[0]["kwargs"]["engine"] == "pyannote_ai"


def test_lite_refuses_to_delete_every_config_because_that_would_mean_local_whisper(
    world, pipeline_env
):
    """``DELETE /all`` clears the active config, and clearing means local Whisper on lite."""
    pipeline_env("lite")
    active = world.add_asr_config("deepgram", "nova-3", active=True)
    world.add_asr_config("assemblyai", "universal")

    response = world.client.delete(f"{_ASR}/all", headers=world.headers)

    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert response.json()["detail"]["code"] == LOCAL_ASR
    assert world.stored("active_asr_config_id") == str(active.id)


def test_the_full_image_still_deletes_every_config(world, pipeline_env):
    """Control: the same request on the full image succeeds, so the refusal is lite-only."""
    pipeline_env("full")
    world.add_asr_config("deepgram", "nova-3", active=True)

    response = world.client.delete(f"{_ASR}/all", headers=world.headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["config_uuid"] is None


def test_lite_with_no_configs_has_nothing_to_delete_and_is_not_refused(world, pipeline_env):
    """Control: with nothing configured the call is a harmless no-op, not a 409."""
    pipeline_env("lite")

    response = world.client.delete(f"{_ASR}/all", headers=world.headers)

    assert response.status_code == status.HTTP_200_OK, response.text
