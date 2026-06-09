"""Characterization tests for ``api/endpoints/admin_timing.py``.

Routes (mounted at ``/api/admin``, guarded by ``get_current_admin_user``):

- ``GET /api/admin/timing/{task_id}``        (merged Redis + Postgres view)
- ``GET /api/admin/timing``                  (recent persisted rows)
- ``GET /api/admin/timing-summary/recent``   (compact projection)

Admin gate: ``role in {admin, super_admin}``. Non-admin → 403 "Not enough
permissions". The ``admin_user`` fixture (role="admin") passes.

Phase-4 regression check (called out explicitly in the plan): ``_row_to_dict``
iterates ``row.__table__.columns``. After the SQLAlchemy 2.0 typed-models
conversion the model uses ``Mapped[...]``/``mapped_column`` — ``__table__`` must
still exist and the column iteration must serialize every field. These tests
create a savepoint-isolated ``FilePipelineTiming`` row and assert the dict
serialization round-trips against the typed model.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import status

from app.api.endpoints.admin_timing import _row_to_dict
from app.models.media import MediaFile
from app.models.pipeline_timing import FilePipelineTiming


def _make_file(db_session, owner) -> MediaFile:
    """Persist a minimal MediaFile (FK target for the timing row's file_id)."""
    file_uuid = str(uuid.uuid4())
    media_file = MediaFile(
        uuid=file_uuid,
        filename="timing_test.wav",
        title="timing_test",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=4096,
        status="completed",
        is_public=False,
        user_id=owner.id,
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


def _make_timing_row(db_session, owner, **overrides) -> FilePipelineTiming:
    """Persist a FilePipelineTiming row on the savepoint-isolated session.

    Rolls back at teardown. ``task_id`` is the string primary key; a unique
    value keeps parallel xdist workers from colliding. ``file_id`` is a NOT
    NULL FK so a backing MediaFile is created first.
    """
    task_id = f"test-timing-{uuid.uuid4()}"
    media_file = _make_file(db_session, owner)
    defaults = {
        "task_id": task_id,
        "file_id": media_file.id,
        "user_id": owner.id,
        "audio_duration_s": 12.5,
        "file_size_bytes": 4096,
        "whisper_model": "large-v3-turbo",
        "asr_provider": "local",
        "http_flow": "upload",
        "user_perceived_duration_ms": 1500,
        "fully_indexed_duration_ms": 2000,
    }
    defaults.update(overrides)
    row = FilePipelineTiming(**defaults)
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


# ---------------------------------------------------------------------------
# Phase-4 typed-models regression: __table__.columns serialization
# ---------------------------------------------------------------------------


def test_row_to_dict_iterates_typed_columns(db_session, normal_user):
    """``_row_to_dict`` must serialize every typed column without error.

    Direct unit check of the dynamic ``row.__table__.columns`` introspection
    that the typed-models conversion (Phase 4) must not break.
    """
    row = _make_timing_row(db_session, normal_user)
    data = _row_to_dict(row)

    # __table__ still present + iterable on the typed declarative model.
    column_names = {c.name for c in FilePipelineTiming.__table__.columns}
    assert column_names <= set(data.keys())
    # Every declared column appears exactly once.
    assert set(data.keys()) == column_names
    assert data["task_id"] == row.task_id
    assert data["whisper_model"] == "large-v3-turbo"
    # created_at is serialized to an ISO string (or None) by the helper.
    assert "created_at" in data
    if data["created_at"] is not None:
        assert isinstance(data["created_at"], str)


# ---------------------------------------------------------------------------
# Auth gates (all three routes share get_current_admin_user)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/api/admin/timing", "/api/admin/timing-summary/recent", "/api/admin/timing/whatever"],
)
def test_timing_unauthorized(client, path):
    """No token → 401."""
    response = client.get(path)
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.parametrize(
    "path",
    ["/api/admin/timing", "/api/admin/timing-summary/recent", "/api/admin/timing/whatever"],
)
def test_timing_non_admin_forbidden(client, user_token_headers, path):
    """A normal user is rejected with the admin-gate detail."""
    response = client.get(path, headers=user_token_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "Not enough permissions"


# ---------------------------------------------------------------------------
# GET /api/admin/timing/{task_id}
# ---------------------------------------------------------------------------


def test_get_timing_not_found(client, admin_token_headers):
    """An unknown task_id with no Redis/Postgres data → 404."""
    missing = f"no-such-task-{uuid.uuid4()}"
    response = client.get(f"/api/admin/timing/{missing}", headers=admin_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == f"No timing data found for task_id={missing}"


def test_get_timing_returns_persisted_row(client, admin_token_headers, admin_user, db_session):
    """A persisted row is returned with the postgres source + serialized fields."""
    row = _make_timing_row(db_session, admin_user)
    response = client.get(f"/api/admin/timing/{row.task_id}", headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["task_id"] == row.task_id
    assert "postgres" in body["source"]
    assert body["persisted"] is not None
    assert body["persisted"]["task_id"] == row.task_id
    assert body["persisted"]["whisper_model"] == "large-v3-turbo"
    # Envelope keys are stable.
    for key in ("task_id", "source", "markers_raw", "derived_from_redis", "persisted"):
        assert key in body


# ---------------------------------------------------------------------------
# GET /api/admin/timing  (list)
# ---------------------------------------------------------------------------


def test_list_timing_envelope(client, admin_token_headers):
    """The list route returns a ``{count, items}`` envelope of serialized rows."""
    response = client.get("/api/admin/timing", headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert "count" in body and "items" in body
    assert isinstance(body["items"], list)
    assert body["count"] == len(body["items"])


def test_list_timing_includes_created_row(client, admin_token_headers, admin_user, db_session):
    """A freshly persisted row shows up in the admin listing."""
    row = _make_timing_row(db_session, admin_user)
    response = client.get("/api/admin/timing?limit=500", headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    task_ids = {item["task_id"] for item in response.json()["items"]}
    assert row.task_id in task_ids


def test_list_timing_user_filter(client, admin_token_headers, admin_user, db_session):
    """The optional ``user_id`` filter scopes the rows returned."""
    row = _make_timing_row(db_session, admin_user)
    response = client.get(f"/api/admin/timing?user_id={admin_user.id}", headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    for item in response.json()["items"]:
        assert item["user_id"] == admin_user.id
    assert row.task_id in {item["task_id"] for item in response.json()["items"]}


def test_list_timing_limit_validation(client, admin_token_headers):
    """``limit`` is bounded 1..500 — out-of-range → 422."""
    too_big = client.get("/api/admin/timing?limit=9999", headers=admin_token_headers)
    assert too_big.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    too_small = client.get("/api/admin/timing?limit=0", headers=admin_token_headers)
    assert too_small.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ---------------------------------------------------------------------------
# GET /api/admin/timing-summary/recent
# ---------------------------------------------------------------------------


def test_timing_summary_envelope(client, admin_token_headers):
    """The compact summary returns a ``{count, items}`` envelope."""
    response = client.get("/api/admin/timing-summary/recent", headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert "count" in body and "items" in body
    assert isinstance(body["items"], list)


def test_timing_summary_projection_fields(client, admin_token_headers, admin_user, db_session):
    """Summary items carry only the headline projection fields."""
    row = _make_timing_row(db_session, admin_user)
    response = client.get("/api/admin/timing-summary/recent?limit=500", headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    items = response.json()["items"]
    match = next((i for i in items if i["task_id"] == row.task_id), None)
    assert match is not None, "freshly persisted row missing from summary"
    for key in (
        "task_id",
        "file_id",
        "user_id",
        "created_at",
        "audio_duration_s",
        "file_size_bytes",
        "whisper_model",
        "asr_provider",
        "http_flow",
    ):
        assert key in match, f"missing summary key {key!r}"
