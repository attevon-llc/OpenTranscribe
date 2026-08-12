"""Behaviour tests for ``/api/user-settings`` toggles and ``*/system-defaults`` (#431).

Split out of ``test_user_settings_routes.py`` to keep both files inside the repo's
~300-line ceiling. Covers three groups no test referenced:

* **``/auto-label``** — no test anywhere in the tree, in this suite or in
  ``test_topics.py`` (which covers the topic routes, not the per-user toggle).
* **``/ai-summary``** — reachable only outside the SPA. ``LLMSettings.svelte`` calls
  ``/settings/ai-summary`` and nothing is mounted at ``/settings``, so the GET 404s
  into a ``console.warn`` and the PUT raises a toast. **The backend route is the
  correct one and is what is pinned here**; the frontend path is the bug (see
  ``backend/app/api/CLAUDE.md``). Fixing the SPA is out of scope for this module.
* **``*/system-defaults``** — these read coded constants in ``app/core/constants.py``
  and DB-backed ``SystemSettings``, never ``.env`` and never the caller's own rows.
  Each is asserted to be unmoved by a per-user PUT.

``/speaker-attributes/system-defaults`` is the one that layers DB over env, so it gets
a matched pair: env decides when the DB is silent, and the DB wins when it is not —
same code path, opposite outcome, driven only by the stubbed settings map.
"""

from __future__ import annotations

import pytest
from fastapi import status

from app.core.constants import DEFAULT_AUTO_LABEL_CONFIDENCE_THRESHOLD
from app.core.constants import DEFAULT_VIDEO_QUALITY

_BASE = "/api/user-settings"

_ROUTES = [
    ("GET", "/ai-summary"),
    ("PUT", "/ai-summary"),
    ("GET", "/auto-label"),
    ("PUT", "/auto-label"),
]


@pytest.mark.parametrize(("method", "path"), _ROUTES, ids=[f"{m} {p}" for m, p in _ROUTES])
def test_route_requires_authentication(client, method, path):
    resp = client.request(method, f"{_BASE}{path}", json={})
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED


# ===========================================================================
# /ai-summary — unreachable from the SPA, correct on the backend
# ===========================================================================


def test_ai_summary_round_trip_both_directions(client, user_token_headers):
    on = client.put(f"{_BASE}/ai-summary", json={"enabled": True}, headers=user_token_headers)
    assert on.status_code == status.HTTP_200_OK
    assert on.json()["message"] == "AI summary auto-generation enabled"

    reread_on = client.get(f"{_BASE}/ai-summary", headers=user_token_headers)
    assert reread_on.status_code == status.HTTP_200_OK
    assert reread_on.json() == {"ai_summary_enabled": True}

    off = client.put(f"{_BASE}/ai-summary", json={"enabled": False}, headers=user_token_headers)
    assert off.status_code == status.HTTP_200_OK
    assert off.json()["ai_summary_enabled"] is False

    reread_off = client.get(f"{_BASE}/ai-summary", headers=user_token_headers)
    assert reread_off.status_code == status.HTTP_200_OK
    assert reread_off.json() == {"ai_summary_enabled": False}


def test_ai_summary_put_missing_body_is_422(client, user_token_headers):
    resp = client.put(f"{_BASE}/ai-summary", json={}, headers=user_token_headers)
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_ai_summary_put_is_scoped_to_the_caller(
    client, user_token_headers, other_user_auth_headers
):
    baseline = client.get(f"{_BASE}/ai-summary", headers=other_user_auth_headers)
    assert baseline.status_code == status.HTTP_200_OK

    mine = client.put(f"{_BASE}/ai-summary", json={"enabled": False}, headers=user_token_headers)
    assert mine.status_code == status.HTTP_200_OK

    mine_reread = client.get(f"{_BASE}/ai-summary", headers=user_token_headers)
    assert mine_reread.status_code == status.HTTP_200_OK
    assert mine_reread.json()["ai_summary_enabled"] is False

    theirs = client.get(f"{_BASE}/ai-summary", headers=other_user_auth_headers)
    assert theirs.status_code == status.HTTP_200_OK
    assert theirs.json() == baseline.json()


# ===========================================================================
# /auto-label — no test referenced this route anywhere in the tree
# ===========================================================================


def test_auto_label_defaults(client, user_token_headers):
    resp = client.get(f"{_BASE}/auto-label", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json() == {
        "enabled": True,
        "confidence_threshold": DEFAULT_AUTO_LABEL_CONFIDENCE_THRESHOLD,
        "tags_enabled": True,
        "collections_enabled": True,
        "bulk_grouping_enabled": True,
    }


def test_auto_label_round_trip(client, user_token_headers):
    put = client.put(
        f"{_BASE}/auto-label",
        json={
            "enabled": False,
            "confidence_threshold": 0.9,
            "tags_enabled": False,
            "collections_enabled": True,
            "bulk_grouping_enabled": False,
        },
        headers=user_token_headers,
    )
    assert put.status_code == status.HTTP_200_OK

    reread = client.get(f"{_BASE}/auto-label", headers=user_token_headers)
    assert reread.status_code == status.HTTP_200_OK
    assert reread.json() == {
        "enabled": False,
        "confidence_threshold": 0.9,
        "tags_enabled": False,
        "collections_enabled": True,
        "bulk_grouping_enabled": False,
    }


def test_auto_label_threshold_below_schema_floor_is_422(client, user_token_headers):
    """``confidence_threshold`` is ``ge=0.5`` on the schema, so 0.1 never reaches the
    service's own 0.0-1.0 check."""
    resp = client.put(
        f"{_BASE}/auto-label",
        json={"enabled": True, "confidence_threshold": 0.1},
        headers=user_token_headers,
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_auto_label_put_is_scoped_to_the_caller(
    client, user_token_headers, other_user_auth_headers
):
    mine = client.put(
        f"{_BASE}/auto-label",
        json={"enabled": False, "confidence_threshold": 0.55},
        headers=user_token_headers,
    )
    assert mine.status_code == status.HTTP_200_OK

    mine_reread = client.get(f"{_BASE}/auto-label", headers=user_token_headers)
    assert mine_reread.status_code == status.HTTP_200_OK
    assert mine_reread.json()["enabled"] is False
    assert mine_reread.json()["confidence_threshold"] == 0.55

    theirs = client.get(f"{_BASE}/auto-label", headers=other_user_auth_headers)
    assert theirs.status_code == status.HTTP_200_OK
    assert theirs.json()["enabled"] is True
    assert theirs.json()["confidence_threshold"] == DEFAULT_AUTO_LABEL_CONFIDENCE_THRESHOLD


# ===========================================================================
# */system-defaults read coded constants / DB SystemSettings, never user rows
# ===========================================================================


def test_download_system_defaults_are_the_coded_constants(client, user_token_headers):
    resp = client.get(f"{_BASE}/download/system-defaults", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["video_quality"] == "best"
    assert data["audio_quality"] == "best"
    assert data["audio_only"] is False
    assert list(data["available_video_qualities"]) == [
        "best",
        "2160p",
        "1440p",
        "1080p",
        "720p",
        "480p",
        "360p",
    ]
    assert list(data["available_audio_qualities"]) == ["best", "320", "192", "128"]


def test_download_system_defaults_ignore_the_users_own_customisation(client, user_token_headers):
    put = client.put(
        f"{_BASE}/download", json={"video_quality": "360p"}, headers=user_token_headers
    )
    assert put.status_code == status.HTTP_200_OK
    assert put.json()["video_quality"] == "360p"

    resp = client.get(f"{_BASE}/download/system-defaults", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["video_quality"] == DEFAULT_VIDEO_QUALITY


def test_transcription_system_defaults_expose_the_valid_option_sets(client, user_token_headers):
    resp = client.get(f"{_BASE}/transcription/system-defaults", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["valid_speaker_prompt_behaviors"] == ["always_prompt", "use_defaults", "use_custom"]
    assert data["valid_diarization_sources"] == ["provider", "local", "pyannote", "off"]
    assert data["diarization_source_default"] == "provider"
    assert data["garbage_cleanup_enabled"] is True
    assert data["garbage_cleanup_threshold"] == 50
    assert data["available_source_languages"]["en"] == "English"


def test_transcription_system_defaults_ignore_the_users_own_customisation(
    client, user_token_headers
):
    before = client.get(f"{_BASE}/transcription/system-defaults", headers=user_token_headers)
    assert before.status_code == status.HTTP_200_OK

    put = client.put(
        f"{_BASE}/transcription",
        json={"min_speakers": 5, "max_speakers": 6, "garbage_cleanup_threshold": 11},
        headers=user_token_headers,
    )
    assert put.status_code == status.HTTP_200_OK
    assert put.json()["garbage_cleanup_threshold"] == 11

    after = client.get(f"{_BASE}/transcription/system-defaults", headers=user_token_headers)
    assert after.status_code == status.HTTP_200_OK
    assert after.json() == before.json()


def test_speaker_attributes_system_defaults_follow_env_when_db_is_unset(
    client, user_token_headers, monkeypatch
):
    """With no ``SystemSettings`` override, the env var decides ``detection_enabled``."""
    monkeypatch.setattr(
        "app.services.system_settings_service.get_settings_map", lambda _db, _keys: {}
    )
    monkeypatch.setenv("SPEAKER_ATTRIBUTE_DETECTION_ENABLED", "false")
    resp = client.get(f"{_BASE}/speaker-attributes/system-defaults", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["detection_enabled"] is False
    assert resp.json()["gender_detection_enabled"] is True


def test_speaker_attributes_system_defaults_prefer_db_over_env(
    client, user_token_headers, monkeypatch
):
    """Control for the test above: same code path, opposite outcome, and it is the DB
    value that wins — the env var says ``false`` and the answer is still ``True``."""
    monkeypatch.setattr(
        "app.services.system_settings_service.get_settings_map",
        lambda _db, _keys: {
            "speaker_attribute.detection_enabled": "true",
            "speaker_attribute.show_on_cards": "false",
        },
    )
    monkeypatch.setenv("SPEAKER_ATTRIBUTE_DETECTION_ENABLED", "false")
    resp = client.get(f"{_BASE}/speaker-attributes/system-defaults", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["detection_enabled"] is True
    assert resp.json()["show_attributes_on_cards"] is False


def test_speaker_attributes_system_defaults_ignore_the_users_own_customisation(
    client, user_token_headers
):
    """Compared before/after rather than to a literal: these values are DB-backed and
    ambient on a live stack, so asserting one would test this deployment's config
    rather than the route."""
    before = client.get(f"{_BASE}/speaker-attributes/system-defaults", headers=user_token_headers)
    assert before.status_code == status.HTTP_200_OK

    put = client.put(
        f"{_BASE}/speaker-attributes",
        json={"detection_enabled": False, "show_attributes_on_cards": False},
        headers=user_token_headers,
    )
    assert put.status_code == status.HTTP_200_OK
    assert put.json()["detection_enabled"] is False

    after = client.get(f"{_BASE}/speaker-attributes/system-defaults", headers=user_token_headers)
    assert after.status_code == status.HTTP_200_OK
    assert after.json() == before.json()
