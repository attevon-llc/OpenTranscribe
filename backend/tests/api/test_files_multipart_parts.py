"""``POST /api/files/multipart/parts`` — the part-signing endpoint (issue #327).

The browser calls this between ``/files/prepare`` and ``/files/complete`` to get
the next batch of signed part URLs, and to ask which parts already landed when
resuming. Everything here is a DB/authorization branch and runs ungated; the
genuine sign → PUT → complete round trip lives in
``tests/unit/test_multipart_upload.py`` behind the MinIO gate.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi import status

from app.models.media import MediaFile
from app.services import multipart_upload

ENDPOINT = "/api/files/multipart/parts"

S3_LIVE = os.environ.get("SKIP_S3", "True").lower() != "true"


def _seed_prepared_file(db_session, owner) -> MediaFile:
    """A prepared (PENDING, storage_path set) row, as /prepare would leave it."""
    file_uuid = str(uuid.uuid4())
    media_file = MediaFile(
        uuid=file_uuid,
        filename="big.mp4",
        title="big",
        storage_path=f"media/test/{file_uuid}.mp4",
        content_type="video/mp4",
        file_size=8 * 1024**3,
        status="pending",
        is_public=False,
        user_id=owner.id,
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


def _payload(file_id: str, **overrides) -> dict:
    body = {"file_id": file_id, "upload_id": "upload-1", "part_numbers": [1, 2]}
    body.update(overrides)
    return body


def test_unauthorized(client):
    response = client.post(ENDPOINT, json=_payload(str(uuid.uuid4())))
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_unknown_file_404(client, user_token_headers):
    response = client.post(ENDPOINT, headers=user_token_headers, json=_payload(str(uuid.uuid4())))
    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_other_users_file_404(client, user_token_headers, other_user, db_session):
    """A signed part URL writes into that row's object key.

    Handing one out for someone else's row would let a caller overwrite another
    user's media, so a foreign row must be indistinguishable from a missing one.
    """
    foreign = _seed_prepared_file(db_session, other_user)
    response = client.post(ENDPOINT, headers=user_token_headers, json=_payload(str(foreign.uuid)))
    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_oversized_batch_rejected(client, user_token_headers, normal_user, db_session):
    """One batch per call — not a wholesale mint of long-lived signed URLs."""
    prepared = _seed_prepared_file(db_session, normal_user)
    response = client.post(
        ENDPOINT,
        headers=user_token_headers,
        json=_payload(str(prepared.uuid), part_numbers=list(range(1, 200))),
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST


def test_empty_batch_signs_nothing(client, user_token_headers, normal_user, db_session):
    """An empty request is how the client asks only for resume state."""
    prepared = _seed_prepared_file(db_session, normal_user)
    response = client.post(
        ENDPOINT, headers=user_token_headers, json=_payload(str(prepared.uuid), part_numbers=[])
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["urls"] == {}


def test_one_url_per_requested_part(
    client, user_token_headers, normal_user, db_session, monkeypatch
):
    """Response shape. Signing itself is stubbed — minio-py resolves the bucket
    region over the network the first time, so real signing needs the stack."""
    monkeypatch.setattr(
        multipart_upload,
        "presign_parts",
        lambda name, upload_id, parts: (
            {n: f"https://example/{name}?partNumber={n}" for n in parts},
            3600,
        ),
    )
    prepared = _seed_prepared_file(db_session, normal_user)
    response = client.post(ENDPOINT, headers=user_token_headers, json=_payload(str(prepared.uuid)))
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert sorted(body["urls"]) == ["1", "2"]
    # The lifetime is the clamped value actually signed, so the client can
    # re-ask before it lapses instead of discovering expiry as a 403.
    assert 0 < body["expires_in"] <= 21600


def test_dead_upload_is_a_409_not_an_empty_resume_list(
    client, user_token_headers, normal_user, db_session, monkeypatch
):
    """An empty list would read as "nothing uploaded yet" and make the client
    re-send every part into an upload_id that no longer exists."""

    def gone(*_args, **_kwargs):
        raise RuntimeError("NoSuchUpload")

    monkeypatch.setattr(multipart_upload, "list_uploaded_parts", gone)
    prepared = _seed_prepared_file(db_session, normal_user)
    response = client.post(
        ENDPOINT,
        headers=user_token_headers,
        json=_payload(str(prepared.uuid), part_numbers=[], include_uploaded=True),
    )
    assert response.status_code == status.HTTP_409_CONFLICT


@pytest.mark.skipif(not S3_LIVE, reason="minio-py resolves the bucket region over the network")
def test_real_signed_urls_carry_the_part_and_upload_id(
    client, user_token_headers, normal_user, db_session
):
    prepared = _seed_prepared_file(db_session, normal_user)
    response = client.post(ENDPOINT, headers=user_token_headers, json=_payload(str(prepared.uuid)))
    assert response.status_code == status.HTTP_200_OK
    for number, url in response.json()["urls"].items():
        assert f"partNumber={number}" in url
        assert "uploadId=upload-1" in url
