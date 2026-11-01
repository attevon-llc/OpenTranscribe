"""Branch pins for the download helpers in ``files/media_delivery.py``.

The download path has three places where the caller's redaction policy must reach the
artifact, and one fail-closed refusal (issue #85): the burned-in-subtitle variant, the
cache key that variant is part of, the dispatch guard and task, and the SSE event filter
that keeps one reader from receiving a render masked under another reader's policy.
Each test here fails if one of those branches is weakened; none needs a database.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api.endpoints.files import media_delivery
from app.models.media import MediaFile

_DB = MagicMock(spec=Session)


def _file(**overrides: Any) -> MediaFile:
    media_file = MediaFile(
        id=4242,
        filename="clip.mp4",
        storage_path="media/test/clip.mp4",
        content_type="video/mp4",
        status="completed",
        redaction_status="done",
    )
    for key, value in overrides.items():
        setattr(media_file, key, value)
    return media_file


class _Policy:
    """Stands in for the resolved redaction config; only its identity is used."""


@pytest.fixture
def policy(monkeypatch) -> dict[str, Any]:
    """Patch the three redaction-policy calls at their home modules; record their inputs."""
    from app.services.redaction import config as redaction_config
    from app.services.redaction import export_policy

    seen: dict[str, Any] = {"pending": False, "resolve_calls": []}
    cfg = _Policy()

    def _resolve(db, user_id, organization_id=None):
        seen["resolve_calls"].append((user_id, organization_id))
        if seen.get("raise"):
            raise RuntimeError("policy store down")
        return cfg

    monkeypatch.setattr(redaction_config, "resolve_effective_config", _resolve)
    monkeypatch.setattr(
        export_policy, "export_masking_is_pending", lambda c, status: seen["pending"]
    )
    monkeypatch.setattr(export_policy, "export_policy_fingerprint", lambda c: "fp-reader")
    return seen


# ---------------------------------------------------------------------------
# _download_redaction_variant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["video_original", "audio_mp3", "audio_wav", "audio_original"])
def test_a_mode_without_transcript_text_resolves_no_policy(policy, mode):
    """Only ``video_subtitles`` carries text; every other mode stays on the empty variant."""
    policy["raise"] = True
    assert media_delivery._download_redaction_variant(_DB, _file(), 7, mode) == ""
    assert policy["resolve_calls"] == []


def test_an_untranscribed_file_has_no_variant_and_resolves_no_policy(policy):
    policy["raise"] = True
    variant = media_delivery._download_redaction_variant(
        _DB,
        _file(status="processing"),
        7,
        "video_subtitles",
    )
    assert variant == ""
    assert policy["resolve_calls"] == []


def test_the_subtitle_variant_is_the_readers_policy_fingerprint_in_their_tenant(policy):
    variant = media_delivery._download_redaction_variant(
        _DB,
        _file(),
        7,
        "video_subtitles",
        organization_id=31,
    )
    assert variant == "fp-reader"
    assert policy["resolve_calls"] == [(7, 31)]


def test_an_unresolvable_policy_withholds_the_render(policy):
    """FAIL CLOSED: a policy that cannot be read must never degrade to "no policy"."""
    policy["raise"] = True
    with pytest.raises(HTTPException) as exc:
        media_delivery._download_redaction_variant(_DB, _file(), 7, "video_subtitles")
    assert exc.value.status_code == 503


def test_a_pending_scan_withholds_the_render(policy):
    policy["pending"] = True
    with pytest.raises(HTTPException) as exc:
        media_delivery._download_redaction_variant(_DB, _file(), 7, "video_subtitles")
    assert exc.value.status_code == 409


# ---------------------------------------------------------------------------
# _resolve_ready_download: the policy is part of the subtitle cache key
# ---------------------------------------------------------------------------


class _Service:
    def __init__(self, *_: Any) -> None:
        pass

    def generate_cache_key(self, file_id, filename, include_speakers, redaction_fingerprint):
        return f"key-{redaction_fingerprint}"

    def is_video_cached(self, cache_key):
        return cache_key == "key-fp-reader"

    def presigned_download_url(self, cache_key, name, content_type):
        return f"https://store/{cache_key}/{name}"


@pytest.fixture
def storage(monkeypatch):
    from app.services import minio_bucket_admin
    from app.services import minio_service
    from app.services import video_processing_service

    monkeypatch.setattr(video_processing_service, "VideoProcessingService", _Service)
    monkeypatch.setattr(minio_bucket_admin, "MinIOService", lambda: None)
    monkeypatch.setattr(
        minio_service,
        "get_presigned_download_url",
        lambda path, download_filename, content_type: f"https://store/{path}",
    )


def test_a_cached_subtitle_render_is_found_only_under_the_readers_policy(storage):
    ready = media_delivery._resolve_ready_download(_file(), "video_subtitles", "fp-reader")
    assert ready == {
        "url": "https://store/key-fp-reader/clip_with_subtitles.mp4",
        "filename": "clip_with_subtitles.mp4",
    }
    assert media_delivery._resolve_ready_download(_file(), "video_subtitles", "") is None


def test_a_subtitle_download_before_the_transcript_is_a_422(storage):
    with pytest.raises(HTTPException) as exc:
        media_delivery._resolve_ready_download(_file(status="processing"), "video_subtitles")
    assert exc.value.status_code == 422


def test_an_audio_mode_never_passes_a_video_original_through(storage):
    """``audio_original`` serves original bytes only when the original IS audio."""
    assert not media_delivery._audio_passthrough(_file(), "audio_original")
    audio = _file(filename="talk.ogg", content_type="audio/ogg")
    assert media_delivery._audio_passthrough(audio, "audio_original")
    assert not media_delivery._audio_passthrough(audio, "audio_mp3")


# ---------------------------------------------------------------------------
# _ensure_prepare_enqueued: one build per (file, mode, variant)
# ---------------------------------------------------------------------------


class _Redis:
    def __init__(self) -> None:
        self.keys: set[str] = set()

    def set(self, key, value, nx, ex):
        if key in self.keys:
            return None
        self.keys.add(key)
        return True


@pytest.fixture
def dispatch(monkeypatch) -> dict[str, Any]:
    from app.core import redis as core_redis
    from app.tasks import media_download

    state: dict[str, Any] = {"redis": _Redis(), "calls": []}

    class _Task:
        @staticmethod
        def delay(**kwargs):
            state["calls"].append(kwargs)

    monkeypatch.setattr(core_redis, "get_redis", lambda: state["redis"])
    monkeypatch.setattr(media_download, "prepare_media_download_task", _Task)
    return state


def test_two_policies_do_not_dedupe_onto_one_build(dispatch):
    media_delivery._ensure_prepare_enqueued(_file(), 7, "video_subtitles", "fp-a")
    media_delivery._ensure_prepare_enqueued(_file(), 8, "video_subtitles", "fp-b")
    media_delivery._ensure_prepare_enqueued(_file(), 9, "video_subtitles", "fp-a")
    assert dispatch["calls"] == [
        {"file_id": 4242, "user_id": 7, "mode": "video_subtitles", "variant": "fp-a"},
        {"file_id": 4242, "user_id": 8, "mode": "video_subtitles", "variant": "fp-b"},
    ]


def test_a_lost_dedup_guard_still_dispatches(dispatch, monkeypatch):
    from app.core import redis as core_redis

    def _down():
        raise ConnectionError("redis down")

    monkeypatch.setattr(core_redis, "get_redis", _down)
    media_delivery._ensure_prepare_enqueued(_file(), 7, "audio_mp3")
    assert dispatch["calls"] == [
        {"file_id": 4242, "user_id": 7, "mode": "audio_mp3", "variant": ""}
    ]


# ---------------------------------------------------------------------------
# _download_event_frame: a reader only ever sees their own render
# ---------------------------------------------------------------------------


def test_an_event_for_another_policys_render_is_ignored():
    event = {"mode": "video_subtitles", "variant": "fp-other", "status": "completed", "url": "u"}
    assert media_delivery._download_event_frame(event, "video_subtitles", "fp-mine") == (
        None,
        False,
    )


def test_an_event_for_another_mode_is_ignored():
    event = {"mode": "audio_mp3", "status": "completed", "url": "u"}
    assert media_delivery._download_event_frame(event, "audio_wav") == (None, False)


def test_the_readers_own_completion_is_a_terminal_ready_frame():
    event = {"mode": "video_subtitles", "variant": "fp-mine", "status": "completed", "url": "u"}
    frame, terminal = media_delivery._download_event_frame(event, "video_subtitles", "fp-mine")
    assert terminal is True
    assert frame == f"event: ready\ndata: {json.dumps({'url': 'u', 'filename': ''})}\n\n"
