"""Characterization tests for the user-settings endpoints.

Covers ``app/api/endpoints/user_settings.py`` mounted at ``/api/user-settings``:
recording, audio-extraction, transcription (+ system-defaults), organization
context (+ sharing), speaker-attributes, download (+ system-defaults),
ai-summary, and per-user media sources (write-only credentials).

These pin the CURRENT observable behavior (status code + detail + round-trip)
across the full GET/PUT/DELETE matrix so later model/dedup/perf refactors can't
change the API by accident. Every write lands on the savepoint-isolated
``db_session`` and rolls back at teardown; the suite-level leak check confirms
no UserSetting drift. The media-source contract pins that the encrypted password
is NEVER returned in any response.
"""

from __future__ import annotations

import uuid

from fastapi import status

_BASE = "/api/user-settings"


# ===========================================================================
# Recording settings
# ===========================================================================


def test_recording_unauthorized(client):
    assert client.get(f"{_BASE}/recording").status_code == status.HTTP_401_UNAUTHORIZED


def test_recording_defaults(client, user_token_headers):
    resp = client.get(f"{_BASE}/recording", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    for key in ("max_recording_duration", "recording_quality", "auto_stop_enabled"):
        assert key in data
    assert isinstance(data["auto_stop_enabled"], bool)


def test_recording_update_round_trip(client, user_token_headers):
    resp = client.put(
        f"{_BASE}/recording",
        json={"recording_quality": "high", "auto_stop_enabled": False},
        headers=user_token_headers,
    )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["recording_quality"] == "high"
    assert data["auto_stop_enabled"] is False


def test_recording_update_invalid_key_is_400(client, user_token_headers):
    resp = client.put(f"{_BASE}/recording", json={"bogus": 1}, headers=user_token_headers)
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "Invalid setting keys" in resp.json()["detail"]


def test_recording_update_invalid_duration_is_400(client, user_token_headers):
    resp = client.put(
        f"{_BASE}/recording",
        json={"max_recording_duration": 999999},
        headers=user_token_headers,
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "max_recording_duration must be one of" in resp.json()["detail"]


def test_recording_update_invalid_quality_is_400(client, user_token_headers):
    resp = client.put(
        f"{_BASE}/recording",
        json={"recording_quality": "ultra-mega"},
        headers=user_token_headers,
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "recording_quality must be one of" in resp.json()["detail"]


def test_recording_update_non_bool_auto_stop_is_400(client, user_token_headers):
    resp = client.put(
        f"{_BASE}/recording",
        json={"auto_stop_enabled": "yes"},
        headers=user_token_headers,
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert resp.json()["detail"] == "auto_stop_enabled must be a boolean"


def test_recording_reset(client, user_token_headers):
    client.put(f"{_BASE}/recording", json={"recording_quality": "high"}, headers=user_token_headers)
    resp = client.delete(f"{_BASE}/recording", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert "default_settings" in resp.json()


# ===========================================================================
# Audio-extraction settings
# ===========================================================================


def test_audio_extraction_defaults(client, user_token_headers):
    resp = client.get(f"{_BASE}/audio-extraction", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    for key in ("auto_extract_enabled", "extraction_threshold_mb", "remember_choice", "show_modal"):
        assert key in data


def test_audio_extraction_update_round_trip(client, user_token_headers):
    resp = client.put(
        f"{_BASE}/audio-extraction",
        json={"extraction_threshold_mb": 250, "show_modal": False},
        headers=user_token_headers,
    )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["extraction_threshold_mb"] == 250
    assert data["show_modal"] is False


def test_audio_extraction_invalid_key_is_400(client, user_token_headers):
    resp = client.put(f"{_BASE}/audio-extraction", json={"bogus": 1}, headers=user_token_headers)
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_audio_extraction_threshold_out_of_range_is_400(client, user_token_headers):
    resp = client.put(
        f"{_BASE}/audio-extraction",
        json={"extraction_threshold_mb": 0},
        headers=user_token_headers,
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "between 1 and 10000" in resp.json()["detail"]


def test_audio_extraction_non_bool_is_400(client, user_token_headers):
    resp = client.put(
        f"{_BASE}/audio-extraction",
        json={"remember_choice": "nope"},
        headers=user_token_headers,
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


# ===========================================================================
# /all (debug dump)
# ===========================================================================


def test_get_all_settings_empty(client, user_token_headers):
    resp = client.get(f"{_BASE}/all", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert isinstance(resp.json(), dict)


def test_get_all_settings_reflects_writes(client, user_token_headers):
    client.put(f"{_BASE}/recording", json={"recording_quality": "high"}, headers=user_token_headers)
    data = client.get(f"{_BASE}/all", headers=user_token_headers).json()
    assert data.get("recording_quality") == "high"


# ===========================================================================
# Transcription settings
# ===========================================================================


def test_transcription_defaults_shape(client, user_token_headers):
    resp = client.get(f"{_BASE}/transcription", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    for key in (
        "min_speakers",
        "max_speakers",
        "speaker_prompt_behavior",
        "garbage_cleanup_enabled",
        "source_language",
        "translate_to_english",
        "llm_output_language",
        "diarization_source",
    ):
        assert key in data


def test_transcription_update_round_trip(client, user_token_headers):
    resp = client.put(
        f"{_BASE}/transcription",
        json={"min_speakers": 2, "max_speakers": 8, "source_language": "es"},
        headers=user_token_headers,
    )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["min_speakers"] == 2
    assert data["max_speakers"] == 8
    assert data["source_language"] == "es"


def test_transcription_min_gt_max_is_400(client, user_token_headers):
    resp = client.put(
        f"{_BASE}/transcription",
        json={"min_speakers": 9, "max_speakers": 3},
        headers=user_token_headers,
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert resp.json()["detail"] == "min_speakers cannot be greater than max_speakers"


def test_transcription_invalid_source_language_is_400(client, user_token_headers):
    resp = client.put(
        f"{_BASE}/transcription",
        json={"source_language": "zz-not-real"},
        headers=user_token_headers,
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "source_language must be a valid ISO 639-1 code" in resp.json()["detail"]


def test_transcription_invalid_llm_output_language_is_400(client, user_token_headers):
    resp = client.put(
        f"{_BASE}/transcription",
        json={"llm_output_language": "zz"},
        headers=user_token_headers,
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "llm_output_language must be one of" in resp.json()["detail"]


def test_transcription_invalid_diarization_source_is_422(client, user_token_headers):
    """diarization_source passes schema (free str) but is rejected at 422 in-handler."""
    resp = client.put(
        f"{_BASE}/transcription",
        json={"diarization_source": "telepathy"},
        headers=user_token_headers,
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert "Invalid diarization_source" in resp.json()["detail"]


def test_transcription_speakers_below_min_is_422(client, user_token_headers):
    """min_speakers Field is ge=1 → schema 422."""
    resp = client.put(
        f"{_BASE}/transcription",
        json={"min_speakers": 0},
        headers=user_token_headers,
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_transcription_vad_threshold_out_of_range_is_422(client, user_token_headers):
    resp = client.put(
        f"{_BASE}/transcription",
        json={"vad_threshold": 2.0},
        headers=user_token_headers,
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_transcription_empty_update_returns_current(client, user_token_headers):
    """An all-None update is a no-op that returns current settings (200)."""
    resp = client.put(f"{_BASE}/transcription", json={}, headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert "min_speakers" in resp.json()


def test_transcription_hallucination_disable_round_trip(client, user_token_headers):
    """Explicitly setting hallucination_silence_threshold then clearing it works."""
    set_resp = client.put(
        f"{_BASE}/transcription",
        json={"hallucination_silence_threshold": 2.0},
        headers=user_token_headers,
    )
    assert set_resp.json()["hallucination_silence_threshold"] == 2.0


def test_transcription_reset(client, user_token_headers):
    client.put(f"{_BASE}/transcription", json={"min_speakers": 3}, headers=user_token_headers)
    resp = client.delete(f"{_BASE}/transcription", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert "default_settings" in resp.json()


def test_transcription_system_defaults_requires_auth(client):
    """The route is authenticated, despite a docstring that long claimed otherwise.

    It carries ``Depends(get_current_active_user)``; the previous version of this
    test asserted 200 for an anonymous caller and had been failing. Pinning the
    implemented behaviour rather than loosening the gate: the payload is only
    configuration, but nothing indicates the dependency was added by accident.
    """
    resp = client.get(f"{_BASE}/transcription/system-defaults")
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED


def test_transcription_system_defaults_shape(client, user_token_headers):
    resp = client.get(f"{_BASE}/transcription/system-defaults", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert "min_speakers" in data
    assert "valid_speaker_prompt_behaviors" in data
    assert "available_source_languages" in data


# ===========================================================================
# Organization context
# ===========================================================================


def test_org_context_defaults(client, user_token_headers):
    resp = client.get(f"{_BASE}/organization-context", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    for key in (
        "context_text",
        "include_in_default_prompts",
        "include_in_custom_prompts",
        "is_shared",
    ):
        assert key in data


def test_org_context_update_round_trip(client, user_token_headers):
    resp = client.put(
        f"{_BASE}/organization-context",
        json={"context_text": "We build widgets.", "include_in_default_prompts": True},
        headers=user_token_headers,
    )
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["context_text"] == "We build widgets."


def test_org_context_shared_listing_shape(client, user_token_headers):
    resp = client.get(f"{_BASE}/organization-context/shared", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert "shared_contexts" in resp.json()


def test_org_context_use_shared_nonexistent_is_404(client, user_token_headers):
    """Using a non-shared/unknown user's context → 404."""
    resp = client.post(
        f"{_BASE}/organization-context/use-shared",
        json={"user_id": 99999999},
        headers=user_token_headers,
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "Shared organization context not found"


def test_org_context_use_shared_clear(client, user_token_headers):
    """Posting user_id=None reverts to own context (200, no error)."""
    resp = client.post(
        f"{_BASE}/organization-context/use-shared",
        json={"user_id": None},
        headers=user_token_headers,
    )
    assert resp.status_code == status.HTTP_200_OK
    assert "context_text" in resp.json()


def test_org_context_cross_user_sharing(
    client, user_token_headers, other_user_auth_headers, normal_user
):
    """normal_user shares; other_user sees and can adopt it."""
    client.put(
        f"{_BASE}/organization-context",
        json={"context_text": "Shared corp context", "is_shared": True},
        headers=user_token_headers,
    )
    listing = client.get(
        f"{_BASE}/organization-context/shared", headers=other_user_auth_headers
    ).json()
    shared = listing["shared_contexts"]
    assert any(c["user_id"] == str(normal_user.id) for c in shared)

    adopt = client.post(
        f"{_BASE}/organization-context/use-shared",
        json={"user_id": normal_user.id},
        headers=other_user_auth_headers,
    )
    assert adopt.status_code == status.HTTP_200_OK


def test_org_context_reset(client, user_token_headers):
    client.put(
        f"{_BASE}/organization-context",
        json={"context_text": "temp"},
        headers=user_token_headers,
    )
    resp = client.delete(f"{_BASE}/organization-context", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert "default_settings" in resp.json()


# ===========================================================================
# Speaker-attribute settings
# ===========================================================================


def test_speaker_attributes_defaults(client, user_token_headers):
    resp = client.get(f"{_BASE}/speaker-attributes", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    for key in (
        "detection_enabled",
        "gender_detection_enabled",
        "age_detection_enabled",
        "show_attributes_on_cards",
    ):
        assert key in data


def test_speaker_attributes_update_round_trip(client, user_token_headers):
    resp = client.put(
        f"{_BASE}/speaker-attributes",
        json={"detection_enabled": False, "show_attributes_on_cards": False},
        headers=user_token_headers,
    )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["detection_enabled"] is False
    assert data["show_attributes_on_cards"] is False


def test_speaker_attributes_reset(client, user_token_headers):
    client.put(
        f"{_BASE}/speaker-attributes",
        json={"detection_enabled": False},
        headers=user_token_headers,
    )
    resp = client.delete(f"{_BASE}/speaker-attributes", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert "default_settings" in resp.json()


def test_speaker_attributes_system_defaults(client, user_token_headers):
    resp = client.get(f"{_BASE}/speaker-attributes/system-defaults", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert "detection_enabled" in resp.json()


# ===========================================================================
# Download settings
# ===========================================================================


def test_download_defaults(client, user_token_headers):
    resp = client.get(f"{_BASE}/download", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    for key in ("video_quality", "audio_only", "audio_quality"):
        assert key in data


def test_download_update_round_trip(client, user_token_headers):
    resp = client.put(
        f"{_BASE}/download",
        json={"audio_only": True},
        headers=user_token_headers,
    )
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["audio_only"] is True


def test_download_invalid_video_quality_is_400(client, user_token_headers):
    resp = client.put(
        f"{_BASE}/download",
        json={"video_quality": "9001p"},
        headers=user_token_headers,
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "video_quality must be one of" in resp.json()["detail"]


def test_download_invalid_audio_quality_is_400(client, user_token_headers):
    resp = client.put(
        f"{_BASE}/download",
        json={"audio_quality": "lossless-ultra"},
        headers=user_token_headers,
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "audio_quality must be one of" in resp.json()["detail"]


def test_download_reset(client, user_token_headers):
    client.put(f"{_BASE}/download", json={"audio_only": True}, headers=user_token_headers)
    resp = client.delete(f"{_BASE}/download", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert "default_settings" in resp.json()


def test_download_system_defaults(client, user_token_headers):
    resp = client.get(f"{_BASE}/download/system-defaults", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert "available_video_qualities" in data
    assert "available_audio_qualities" in data


# ===========================================================================
# AI-summary toggle
# ===========================================================================


def test_ai_summary_get_default(client, user_token_headers):
    resp = client.get(f"{_BASE}/ai-summary", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert "ai_summary_enabled" in resp.json()


def test_ai_summary_toggle_round_trip(client, user_token_headers):
    resp = client.put(f"{_BASE}/ai-summary", json={"enabled": True}, headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["ai_summary_enabled"] is True
    assert (
        client.get(f"{_BASE}/ai-summary", headers=user_token_headers).json()["ai_summary_enabled"]
        is True
    )


# ===========================================================================
# Media sources (per-user, write-only credentials)
# ===========================================================================


def test_media_sources_empty(client, user_token_headers):
    resp = client.get(f"{_BASE}/media-sources", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json() == {"sources": [], "shared_sources": []}


def test_media_source_create_password_never_returned(client, user_token_headers):
    """The encrypted password is write-only — no response surface echoes it."""
    secret = "super-secret-pw-12345"  # noqa: S105 - test fixture, not a real credential
    resp = client.post(
        f"{_BASE}/media-sources",
        json={
            "hostname": f"host-{uuid.uuid4().hex[:8]}.example.com",
            "provider_type": "mediacms",
            "username": "alice",
            "password": secret,
            "verify_ssl": True,
        },
        headers=user_token_headers,
    )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["has_credentials"] is True
    assert secret not in resp.text
    assert "password" not in data

    # And the listing also never returns the secret
    listing = client.get(f"{_BASE}/media-sources", headers=user_token_headers)
    assert secret not in listing.text


def test_media_source_duplicate_hostname_is_400(client, user_token_headers):
    host = f"dup-{uuid.uuid4().hex[:8]}.example.com"
    payload = {"hostname": host, "provider_type": "mediacms", "verify_ssl": True}
    first = client.post(f"{_BASE}/media-sources", json=payload, headers=user_token_headers)
    assert first.status_code == status.HTTP_200_OK
    second = client.post(f"{_BASE}/media-sources", json=payload, headers=user_token_headers)
    assert second.status_code == status.HTTP_400_BAD_REQUEST
    assert "already exists" in second.json()["detail"]


def test_media_source_update_other_user_404(client, user_token_headers, other_user_auth_headers):
    """A media source is invisible (404) to a non-owner on update."""
    created = client.post(
        f"{_BASE}/media-sources",
        json={
            "hostname": f"own-{uuid.uuid4().hex[:8]}.example.com",
            "provider_type": "mediacms",
            "verify_ssl": True,
        },
        headers=user_token_headers,
    ).json()
    resp = client.put(
        f"{_BASE}/media-sources/{created['uuid']}",
        json={"label": "hijack"},
        headers=other_user_auth_headers,
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "Media source not found"


def test_media_source_delete_round_trip(client, user_token_headers):
    created = client.post(
        f"{_BASE}/media-sources",
        json={
            "hostname": f"del-{uuid.uuid4().hex[:8]}.example.com",
            "provider_type": "mediacms",
            "verify_ssl": True,
        },
        headers=user_token_headers,
    ).json()
    resp = client.delete(f"{_BASE}/media-sources/{created['uuid']}", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json() == {"success": True}


def test_media_source_delete_nonexistent_404(client, user_token_headers):
    resp = client.delete(f"{_BASE}/media-sources/{uuid.uuid4()}", headers=user_token_headers)
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "Media source not found"
