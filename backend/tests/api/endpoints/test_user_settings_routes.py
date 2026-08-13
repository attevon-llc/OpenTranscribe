"""Behaviour tests for the per-user settings groups (issue #431).

``tests/api/test_user_settings.py`` already exercises parts of this surface, but it
builds every URL from an ``_BASE`` f-string, so a path-literal scan of the test tree
reports these routes as uncovered. This module closes the gaps that scan pointed at:

* **Cross-user isolation.** Every handler here filters ``UserSetting`` by
  ``current_user.id``. Nothing asserted that. A PUT by user A must not move what
  user B's GET returns, and A's DELETE must not wipe B's rows.
* **Every isolation test carries a positive control.** "B is unchanged" passes on
  its own if the GET is broken and *always* answers the coded default, so each test
  also re-GETs as **A** and asserts A's value did move. Same route, same request
  shape, opposite outcome, driven only by which token is presented — so both failure
  modes (a leak, and a read that ignores stored rows entirely) fail the test.
  ``test_transcription_read_is_row_driven_per_user`` is the strongest form: it writes
  a row for B **directly** and asserts only B's read sees it.
* **The unauthenticated matrix**, one exact status code per route: 401.

Three sibling modules split the rest of the surface so each file stays inside the
repo's ~300-line ceiling: round trips and revert-to-default in
``test_user_settings_roundtrip_routes.py``; ``*/system-defaults``, ``/auto-label``
and ``/ai-summary`` in ``test_user_settings_prefs_routes.py``;
``/organization-context*`` in ``test_user_settings_org_context_routes.py``.

Bearer auth is deliberate: ``middleware/csrf.py`` exempts ``Authorization: Bearer``
requests, so the mutating calls need no double-submit token (pinned by
``test_auth_endpoints.py``). ``/user-settings`` is mounted in ``api/router.py`` with
no ``capability=`` argument, so an anonymous request is **401**, not the 404 that
``require_capability`` produces. All writes land on the savepoint-isolated
``db_session`` and roll back.
"""

from __future__ import annotations

import pytest
from fastapi import status

from app.core.constants import DEFAULT_VIDEO_QUALITY

_BASE = "/api/user-settings"


# ===========================================================================
# Unauthenticated: every route in scope answers 401 — not 403, not 404
# ===========================================================================

_ROUTES = [
    ("GET", "/all"),
    ("GET", "/audio-extraction"),
    ("PUT", "/audio-extraction"),
    ("GET", "/download"),
    ("PUT", "/download"),
    ("DELETE", "/download"),
    ("GET", "/download/system-defaults"),
    ("GET", "/recording"),
    ("PUT", "/recording"),
    ("DELETE", "/recording"),
    ("GET", "/speaker-attributes"),
    ("PUT", "/speaker-attributes"),
    ("DELETE", "/speaker-attributes"),
    ("GET", "/speaker-attributes/system-defaults"),
    ("GET", "/transcription"),
    ("PUT", "/transcription"),
    ("DELETE", "/transcription"),
    ("GET", "/transcription/system-defaults"),
]


@pytest.mark.parametrize(("method", "path"), _ROUTES, ids=[f"{m} {p}" for m, p in _ROUTES])
def test_route_requires_authentication(client, method, path):
    """No credentials at all → 401 before any body validation runs."""
    resp = client.request(method, f"{_BASE}{path}", json={})
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED


# ===========================================================================
# Cross-user isolation — the invariant this surface had no test for
# ===========================================================================


def test_transcription_put_is_scoped_to_the_caller(
    client, user_token_headers, other_user_auth_headers
):
    before = client.get(f"{_BASE}/transcription", headers=other_user_auth_headers)
    assert before.status_code == status.HTTP_200_OK

    mine = client.put(
        f"{_BASE}/transcription",
        json={"min_speakers": 3, "max_speakers": 4, "source_language": "de"},
        headers=user_token_headers,
    )
    assert mine.status_code == status.HTTP_200_OK

    # Positive control: the same GET, as A, DID move.
    mine_reread = client.get(f"{_BASE}/transcription", headers=user_token_headers)
    assert mine_reread.status_code == status.HTTP_200_OK
    assert mine_reread.json()["source_language"] == "de"
    assert mine_reread.json()["max_speakers"] == 4

    after = client.get(f"{_BASE}/transcription", headers=other_user_auth_headers)
    assert after.status_code == status.HTTP_200_OK
    assert after.json() == before.json()


def test_download_put_is_scoped_to_the_caller(client, user_token_headers, other_user_auth_headers):
    before = client.get(f"{_BASE}/download", headers=other_user_auth_headers)
    assert before.status_code == status.HTTP_200_OK
    assert before.json()["video_quality"] == DEFAULT_VIDEO_QUALITY

    mine = client.put(
        f"{_BASE}/download",
        json={"video_quality": "480p", "audio_only": True, "audio_quality": "128"},
        headers=user_token_headers,
    )
    assert mine.status_code == status.HTTP_200_OK

    mine_reread = client.get(f"{_BASE}/download", headers=user_token_headers)
    assert mine_reread.status_code == status.HTTP_200_OK
    assert mine_reread.json()["video_quality"] == "480p"
    assert mine_reread.json()["audio_only"] is True

    after = client.get(f"{_BASE}/download", headers=other_user_auth_headers)
    assert after.status_code == status.HTTP_200_OK
    assert after.json()["video_quality"] == DEFAULT_VIDEO_QUALITY
    assert after.json()["audio_only"] is False


def test_speaker_attributes_put_is_scoped_to_the_caller(
    client, user_token_headers, other_user_auth_headers
):
    before = client.get(f"{_BASE}/speaker-attributes", headers=other_user_auth_headers)
    assert before.status_code == status.HTTP_200_OK
    assert before.json()["detection_enabled"] is True

    mine = client.put(
        f"{_BASE}/speaker-attributes",
        json={"detection_enabled": False, "gender_detection_enabled": False},
        headers=user_token_headers,
    )
    assert mine.status_code == status.HTTP_200_OK

    mine_reread = client.get(f"{_BASE}/speaker-attributes", headers=user_token_headers)
    assert mine_reread.status_code == status.HTTP_200_OK
    assert mine_reread.json()["detection_enabled"] is False
    assert mine_reread.json()["gender_detection_enabled"] is False

    after = client.get(f"{_BASE}/speaker-attributes", headers=other_user_auth_headers)
    assert after.status_code == status.HTTP_200_OK
    assert after.json()["detection_enabled"] is True
    assert after.json()["gender_detection_enabled"] is True


def test_transcription_read_is_row_driven_per_user(
    client, db_session, other_user, user_token_headers, other_user_auth_headers
):
    """A row stored for B is visible to B and to nobody else.

    Written straight to ``UserSetting`` rather than through the API so the read side
    is tested on its own: if ``get_transcription_settings`` dropped its ``user_id``
    predicate, A's GET would report 9 as well.
    """
    from app import models

    defaults = client.get(f"{_BASE}/transcription/system-defaults", headers=user_token_headers)
    assert defaults.status_code == status.HTTP_200_OK
    system_max = defaults.json()["max_speakers"]
    injected = system_max + 1  # never equal to the default, so the test can't go vacuous

    db_session.add(
        models.UserSetting(
            user_id=other_user.id,
            setting_key="transcription_max_speakers",
            setting_value=str(injected),
        )
    )
    db_session.commit()

    theirs = client.get(f"{_BASE}/transcription", headers=other_user_auth_headers)
    assert theirs.status_code == status.HTTP_200_OK
    assert theirs.json()["max_speakers"] == injected

    mine = client.get(f"{_BASE}/transcription", headers=user_token_headers)
    assert mine.status_code == status.HTTP_200_OK
    assert mine.json()["max_speakers"] == system_max


def test_download_delete_does_not_reset_other_users_settings(
    client, user_token_headers, other_user_auth_headers
):
    """A reset is scoped to the caller: B's customisation survives A's DELETE."""
    theirs = client.put(
        f"{_BASE}/download", json={"audio_quality": "192"}, headers=other_user_auth_headers
    )
    assert theirs.status_code == status.HTTP_200_OK

    mine_reset = client.delete(f"{_BASE}/download", headers=user_token_headers)
    assert mine_reset.status_code == status.HTTP_200_OK

    after = client.get(f"{_BASE}/download", headers=other_user_auth_headers)
    assert after.status_code == status.HTTP_200_OK
    assert after.json()["audio_quality"] == "192"


def test_speaker_attributes_delete_does_not_reset_other_users_settings(
    client, user_token_headers, other_user_auth_headers
):
    theirs = client.put(
        f"{_BASE}/speaker-attributes",
        json={"age_detection_enabled": False},
        headers=other_user_auth_headers,
    )
    assert theirs.status_code == status.HTTP_200_OK

    mine_reset = client.delete(f"{_BASE}/speaker-attributes", headers=user_token_headers)
    assert mine_reset.status_code == status.HTTP_200_OK

    after = client.get(f"{_BASE}/speaker-attributes", headers=other_user_auth_headers)
    assert after.status_code == status.HTTP_200_OK
    assert after.json()["age_detection_enabled"] is False


def test_all_dump_is_scoped_to_the_caller(client, user_token_headers, other_user_auth_headers):
    mine = client.put(
        f"{_BASE}/recording", json={"recording_quality": "maximum"}, headers=user_token_headers
    )
    assert mine.status_code == status.HTTP_200_OK

    theirs = client.get(f"{_BASE}/all", headers=other_user_auth_headers)
    assert theirs.status_code == status.HTTP_200_OK
    assert "recording_quality" not in theirs.json()

    ours = client.get(f"{_BASE}/all", headers=user_token_headers)
    assert ours.status_code == status.HTTP_200_OK
    assert ours.json()["recording_quality"] == "maximum"
