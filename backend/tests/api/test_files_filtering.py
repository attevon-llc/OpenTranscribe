"""Characterization tests for ``files/filtering.py``.

The filter helpers have no routes of their own — they back ``GET /api/files``
(query params) and ``GET /api/files/metadata-filters``. These tests exercise the
filter combinations through those endpoints against savepoint-isolated rows, so
the behavior of each ``apply_*_filter`` is pinned end-to-end. Transcript-search
(OpenSearch) is intentionally NOT asserted on content here — it degrades
gracefully and the dev index isn't seeded for the test users; the empty-result
contract is covered instead.
"""

from __future__ import annotations

import uuid

from fastapi import status

from app.models.media import MediaFile


def _make_file(db_session, owner, **overrides) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    defaults = {
        "uuid": file_uuid,
        "filename": f"filt_{file_uuid[:8]}.wav",
        "title": "filt_test",
        "storage_path": f"media/test/{file_uuid}.wav",
        "content_type": "audio/wav",
        "file_size": 4096,
        "status": "completed",
        "duration": 60.0,
        "is_public": False,
        "user_id": owner.id,
    }
    defaults.update(overrides)
    media_file = MediaFile(**defaults)
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


def _uuids(response) -> set[str]:
    return {item["uuid"] for item in response.json()["items"]}


# ---------------------------------------------------------------------------
# search filter (filename / title)
# ---------------------------------------------------------------------------


def test_filter_search_by_filename(client, user_token_headers, normal_user, db_session):
    target = _make_file(db_session, normal_user, filename="unique_needle_abc.wav")
    _make_file(db_session, normal_user, filename="other_haystack.wav")
    response = client.get(
        "/api/files", headers=user_token_headers, params={"search": "unique_needle_abc"}
    )
    assert response.status_code == status.HTTP_200_OK
    assert str(target.uuid) in _uuids(response)


def test_filter_search_like_metachars_literal(client, user_token_headers, normal_user, db_session):
    """LIKE metacharacters in the search term are escaped (treated literally)."""
    _make_file(db_session, normal_user, filename="plain.wav")
    response = client.get(
        "/api/files", headers=user_token_headers, params={"search": "%_unmatchable_%"}
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["items"] == []


# ---------------------------------------------------------------------------
# file_type filter
# ---------------------------------------------------------------------------


def test_filter_file_type_audio(client, user_token_headers, normal_user, db_session):
    audio = _make_file(db_session, normal_user, content_type="audio/wav")
    video = _make_file(db_session, normal_user, content_type="video/mp4")
    response = client.get("/api/files", headers=user_token_headers, params={"file_type": "audio"})
    found = _uuids(response)
    assert str(audio.uuid) in found
    assert str(video.uuid) not in found


# ---------------------------------------------------------------------------
# duration filter
# ---------------------------------------------------------------------------


def test_filter_duration_range(client, user_token_headers, normal_user, db_session):
    short = _make_file(db_session, normal_user, duration=10.0)
    long = _make_file(db_session, normal_user, duration=600.0)
    response = client.get(
        "/api/files",
        headers=user_token_headers,
        params={"min_duration": 100, "max_duration": 1000},
    )
    found = _uuids(response)
    assert str(long.uuid) in found
    assert str(short.uuid) not in found


# ---------------------------------------------------------------------------
# file_size filter (MB → bytes conversion)
# ---------------------------------------------------------------------------


def test_filter_file_size_mb(client, user_token_headers, normal_user, db_session):
    small = _make_file(db_session, normal_user, file_size=1024)  # ~1KB
    big = _make_file(db_session, normal_user, file_size=50 * 1024 * 1024)  # 50MB
    response = client.get("/api/files", headers=user_token_headers, params={"min_file_size": 10})
    found = _uuids(response)
    assert str(big.uuid) in found
    assert str(small.uuid) not in found


# ---------------------------------------------------------------------------
# status filter
# ---------------------------------------------------------------------------


def test_filter_status_multi(client, user_token_headers, normal_user, db_session):
    completed = _make_file(db_session, normal_user, status="completed")
    errored = _make_file(db_session, normal_user, status="error")
    response = client.get("/api/files", headers=user_token_headers, params={"status": ["error"]})
    found = _uuids(response)
    assert str(errored.uuid) in found
    assert str(completed.uuid) not in found


# ---------------------------------------------------------------------------
# combined filters
# ---------------------------------------------------------------------------


def test_filter_combined_type_and_status(client, user_token_headers, normal_user, db_session):
    match = _make_file(
        db_session, normal_user, content_type="audio/wav", status="completed", duration=120.0
    )
    _make_file(db_session, normal_user, content_type="video/mp4", status="completed")
    _make_file(db_session, normal_user, content_type="audio/wav", status="error")
    response = client.get(
        "/api/files",
        headers=user_token_headers,
        params={"file_type": "audio", "status": ["completed"], "min_duration": 60},
    )
    found = _uuids(response)
    assert str(match.uuid) in found
    # Every returned item must satisfy all filters.
    for item in response.json()["items"]:
        assert item["status"] == "completed"


def test_filter_date_range_excludes_old(client, user_token_headers, normal_user, db_session):
    """A future from_date excludes everything (upload_time defaults to ~now)."""
    _make_file(db_session, normal_user)
    response = client.get(
        "/api/files", headers=user_token_headers, params={"from_date": "2999-01-01T00:00:00"}
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["items"] == []


# ---------------------------------------------------------------------------
# GET /api/files/metadata-filters
# ---------------------------------------------------------------------------


def test_metadata_filters_shape(client, user_token_headers, normal_user, db_session):
    _make_file(db_session, normal_user, duration=42.0)
    response = client.get("/api/files/metadata-filters", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    for key in ("formats", "codecs", "duration", "file_size", "resolution"):
        assert key in body
    assert set(body["duration"]) == {"min", "max"}
    assert set(body["resolution"]) == {"width", "height"}


def test_metadata_filters_bad_ownership_422(client, user_token_headers):
    response = client.get(
        "/api/files/metadata-filters",
        headers=user_token_headers,
        params={"ownership": "bogus"},
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_metadata_filters_unauthorized(client):
    response = client.get("/api/files/metadata-filters")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_metadata_filters_excludes_quarantined_facets(
    client, user_token_headers, normal_user, db_session
):
    """A2: a quarantined file's language must not leak into the filter facets
    for an ordinary user — the file itself already 404s everywhere else."""
    _make_file(db_session, normal_user, language="qz", is_quarantined=True)
    response = client.get("/api/files/metadata-filters", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert "qz" not in response.json()["languages"]


def test_metadata_filters_admin_sees_quarantined_facets(
    client, admin_token_headers, admin_user, db_session
):
    """Control: an admin (who reviews takedowns) still sees the facet value."""
    _make_file(db_session, admin_user, language="qz", is_quarantined=True)
    response = client.get("/api/files/metadata-filters", headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert "qz" in response.json()["languages"]
