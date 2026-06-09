"""Characterization tests for ``api/endpoints/system.py``.

Routes (mounted at ``/api/system``):

- ``GET /api/system/capabilities``               (anonymous — feature gating)
- ``GET /api/system/stats``                       (any authenticated user)
- ``GET /api/system/config/protected-media-auth`` (any authenticated user)

These pin the CURRENT observable behavior (status code + envelope shape) so
the model/dedup/perf refactors can't change the API by accident. They never
mutate dev data — only reads.

The ``_device_mode_info`` helper has its own focused suite in
``test_device_mode_info.py``; this file covers the HTTP surface and the
``/stats`` aggregation envelope against the live stack.
"""

from __future__ import annotations

from fastapi import status

# ---------------------------------------------------------------------------
# GET /api/system/capabilities  (anonymous)
# ---------------------------------------------------------------------------


def test_capabilities_anonymous_ok(client):
    """Capabilities is unauthenticated — the SPA bootstraps from it before login."""
    response = client.get("/api/system/capabilities")
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert set(body.keys()) >= {"edition", "capabilities", "audience"}
    assert isinstance(body["capabilities"], dict)
    assert isinstance(body["audience"], dict)
    assert isinstance(body["edition"], str)


def test_capabilities_with_auth_ok(client, user_token_headers):
    """An authenticated user gets the same payload shape."""
    response = client.get("/api/system/capabilities", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert "edition" in body
    assert "capabilities" in body


# ---------------------------------------------------------------------------
# GET /api/system/stats  (authenticated)
# ---------------------------------------------------------------------------


def test_stats_unauthorized(client):
    """Stats requires a token."""
    response = client.get("/api/system/stats")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_stats_authenticated_envelope(client, user_token_headers):
    """Any authenticated user can read stats; the top-level envelope is stable.

    The endpoint aggregates DB stats (savepoint DB is reachable in tests) plus
    host system metrics (psutil) — both available without MinIO/OpenSearch.
    """
    response = client.get("/api/system/stats", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    for key in (
        "users",
        "files",
        "transcripts",
        "speakers",
        "models",
        "system",
        "tasks",
        "throughput",
        "eta",
        "file_timing",
        "queues",
    ):
        assert key in body, f"missing stats key {key!r}"


def test_stats_system_block_has_device_mode_fields(client, user_token_headers):
    """The ``system`` sub-block carries version + device-mode fields the UI banner reads."""
    response = client.get("/api/system/stats", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    system_block = response.json()["system"]
    for key in (
        "version",
        "uptime",
        "memory",
        "cpu",
        "disk",
        "gpus",
        "platform",
        "python_version",
        "device_mode",
        "force_cpu_mode",
        "whisper_model",
        "diarization_enabled",
    ):
        assert key in system_block, f"missing system key {key!r}"
    assert system_block["device_mode"] in ("cpu", "cuda")
    assert isinstance(system_block["gpus"], list)


def test_stats_files_and_speakers_blocks_typed(client, user_token_headers):
    """File/speaker aggregate sub-blocks expose their documented numeric fields."""
    response = client.get("/api/system/stats", headers=user_token_headers)
    body = response.json()
    assert "total" in body["files"]
    assert "segments" in body["files"]
    assert "total" in body["speakers"]
    assert "avg_per_file" in body["speakers"]
    assert "total_segments" in body["transcripts"]


# ---------------------------------------------------------------------------
# GET /api/system/config/protected-media-auth  (authenticated)
# ---------------------------------------------------------------------------


def test_protected_media_auth_unauthorized(client):
    """The protected-media auth config requires authentication."""
    response = client.get("/api/system/config/protected-media-auth")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_protected_media_auth_returns_list(client, user_token_headers):
    """Returns a JSON list of provider auth descriptors (never secrets)."""
    response = client.get("/api/system/config/protected-media-auth", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert isinstance(body, list)
