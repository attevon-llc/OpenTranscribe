"""Round-trip and revert-to-default tests for the per-user settings groups (#431).

Split out of ``test_user_settings_routes.py`` (which keeps the unauthenticated matrix
and the cross-user isolation invariants) to stay inside the repo's ~300-line ceiling.

The point of every test here is the **second request**: each PUT handler returns its
own freshly-read state, so a PUT-only assertion cannot tell "persisted" from "echoed".
Each group therefore PUTs, re-GETs, DELETEs, and re-GETs again — and where a
``*/system-defaults`` route exists the post-reset value is compared against **that
route's** answer rather than a copy of the constant, so the two cannot drift apart.

The two rejection tests are the boundary controls: without them a handler that
accepted anything would pass every round trip above.
"""

from __future__ import annotations

from fastapi import status

from app.core.constants import DEFAULT_AUDIO_QUALITY
from app.core.constants import DEFAULT_RECORDING_MAX_DURATION
from app.core.constants import DEFAULT_RECORDING_QUALITY
from app.core.constants import DEFAULT_VIDEO_QUALITY

_BASE = "/api/user-settings"


def test_download_round_trip_and_reset_to_system_default(client, user_token_headers):
    put = client.put(
        f"{_BASE}/download",
        json={"video_quality": "720p", "audio_only": True, "audio_quality": "320"},
        headers=user_token_headers,
    )
    assert put.status_code == status.HTTP_200_OK

    reread = client.get(f"{_BASE}/download", headers=user_token_headers)
    assert reread.status_code == status.HTTP_200_OK
    assert reread.json() == {
        "video_quality": "720p",
        "audio_only": True,
        "audio_quality": "320",
    }

    reset = client.delete(f"{_BASE}/download", headers=user_token_headers)
    assert reset.status_code == status.HTTP_200_OK
    assert reset.json()["default_settings"]["video_quality"] == DEFAULT_VIDEO_QUALITY

    defaults = client.get(f"{_BASE}/download/system-defaults", headers=user_token_headers)
    assert defaults.status_code == status.HTTP_200_OK
    after = client.get(f"{_BASE}/download", headers=user_token_headers)
    assert after.status_code == status.HTTP_200_OK
    assert after.json() == {
        "video_quality": defaults.json()["video_quality"],
        "audio_only": defaults.json()["audio_only"],
        "audio_quality": defaults.json()["audio_quality"],
    }
    assert after.json()["audio_quality"] == DEFAULT_AUDIO_QUALITY


def test_download_put_rejects_an_unknown_field(client, user_token_headers):
    """A typo must not read as success.

    This test originally pinned the *defect*: `DownloadSettingsUpdate` had no
    ``extra="forbid"``, so Pydantic dropped `videoQuality`, `update_data` came back empty
    and the handler answered **200** with the settings unchanged — a client with a
    camelCase typo got a success response and silently no change, and the handler's own
    ``Unknown download setting field`` 422 was unreachable. The schema now forbids extras,
    so the rejection is real and this asserts it.
    """
    seed = client.put(
        f"{_BASE}/download", json={"video_quality": "1080p"}, headers=user_token_headers
    )
    assert seed.status_code == status.HTTP_200_OK

    resp = client.put(
        f"{_BASE}/download", json={"videoQuality": "360p"}, headers=user_token_headers
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    # ...and the rejected request changed nothing.
    after = client.get(f"{_BASE}/download", headers=user_token_headers)
    assert after.status_code == status.HTTP_200_OK
    assert after.json()["video_quality"] == "1080p"


def test_transcription_round_trip_and_reset_to_system_default(client, user_token_headers):
    defaults = client.get(f"{_BASE}/transcription/system-defaults", headers=user_token_headers)
    assert defaults.status_code == status.HTTP_200_OK
    system_min = defaults.json()["min_speakers"]

    put = client.put(
        f"{_BASE}/transcription",
        json={"min_speakers": 2, "max_speakers": 7, "garbage_cleanup_threshold": 33},
        headers=user_token_headers,
    )
    assert put.status_code == status.HTTP_200_OK

    reread = client.get(f"{_BASE}/transcription", headers=user_token_headers)
    assert reread.status_code == status.HTTP_200_OK
    assert reread.json()["min_speakers"] == 2
    assert reread.json()["max_speakers"] == 7
    assert reread.json()["garbage_cleanup_threshold"] == 33

    reset = client.delete(f"{_BASE}/transcription", headers=user_token_headers)
    assert reset.status_code == status.HTTP_200_OK

    after = client.get(f"{_BASE}/transcription", headers=user_token_headers)
    assert after.status_code == status.HTTP_200_OK
    assert after.json()["min_speakers"] == system_min
    assert after.json()["garbage_cleanup_threshold"] == defaults.json()["garbage_cleanup_threshold"]


def test_transcription_put_rejects_an_inverted_speaker_range(client, user_token_headers):
    resp = client.put(
        f"{_BASE}/transcription",
        json={"min_speakers": 6, "max_speakers": 2},
        headers=user_token_headers,
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert resp.json()["detail"] == "min_speakers cannot be greater than max_speakers"


def test_speaker_attributes_round_trip_and_reset(client, user_token_headers):
    put = client.put(
        f"{_BASE}/speaker-attributes",
        json={"age_detection_enabled": False, "show_attributes_on_cards": False},
        headers=user_token_headers,
    )
    assert put.status_code == status.HTTP_200_OK

    reread = client.get(f"{_BASE}/speaker-attributes", headers=user_token_headers)
    assert reread.status_code == status.HTTP_200_OK
    assert reread.json()["age_detection_enabled"] is False
    assert reread.json()["show_attributes_on_cards"] is False

    reset = client.delete(f"{_BASE}/speaker-attributes", headers=user_token_headers)
    assert reset.status_code == status.HTTP_200_OK

    after = client.get(f"{_BASE}/speaker-attributes", headers=user_token_headers)
    assert after.status_code == status.HTTP_200_OK
    assert after.json()["age_detection_enabled"] is True
    assert after.json()["show_attributes_on_cards"] is True


def test_recording_round_trip_and_reset(client, user_token_headers):
    put = client.put(
        f"{_BASE}/recording",
        json={"max_recording_duration": 240, "recording_quality": "standard"},
        headers=user_token_headers,
    )
    assert put.status_code == status.HTTP_200_OK

    reread = client.get(f"{_BASE}/recording", headers=user_token_headers)
    assert reread.status_code == status.HTTP_200_OK
    assert reread.json()["max_recording_duration"] == 240
    assert reread.json()["recording_quality"] == "standard"

    reset = client.delete(f"{_BASE}/recording", headers=user_token_headers)
    assert reset.status_code == status.HTTP_200_OK

    after = client.get(f"{_BASE}/recording", headers=user_token_headers)
    assert after.status_code == status.HTTP_200_OK
    assert after.json()["max_recording_duration"] == DEFAULT_RECORDING_MAX_DURATION
    assert after.json()["recording_quality"] == DEFAULT_RECORDING_QUALITY


def test_recording_put_rejects_a_duration_outside_the_allowed_set(client, user_token_headers):
    resp = client.put(
        f"{_BASE}/recording", json={"max_recording_duration": 37}, headers=user_token_headers
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "max_recording_duration must be one of" in resp.json()["detail"]


def test_audio_extraction_round_trip(client, user_token_headers):
    put = client.put(
        f"{_BASE}/audio-extraction",
        json={"auto_extract_enabled": False, "extraction_threshold_mb": 512},
        headers=user_token_headers,
    )
    assert put.status_code == status.HTTP_200_OK

    reread = client.get(f"{_BASE}/audio-extraction", headers=user_token_headers)
    assert reread.status_code == status.HTTP_200_OK
    assert reread.json()["auto_extract_enabled"] is False
    assert reread.json()["extraction_threshold_mb"] == 512
    assert reread.json()["show_modal"] is True


def test_audio_extraction_put_rejects_an_unknown_key(client, user_token_headers):
    """Unlike ``/download``, this handler takes a raw ``dict`` and validates the key
    set itself, so an unknown key really is a 400."""
    resp = client.put(f"{_BASE}/audio-extraction", json={"nope": True}, headers=user_token_headers)
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "Invalid setting keys" in resp.json()["detail"]
