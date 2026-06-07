"""Characterization tests for ``api/endpoints/transcript_segments.py``.

Wave-3 (speakers domain). Pins the CURRENT observable behavior of the single
endpoint in this module:

- ``PUT /api/transcripts/segments/{segment_uuid}/speaker``  (reassign / unassign)

The ownership contracts for this endpoint (403 "Not authorized to modify this
transcript segment", admin/owner reachability of the inner speaker check) are
already pinned in ``test_ownership_contracts.py``; this module builds the
functional coverage *around* those — 401/404/400/422 paths, the unassign branch,
the same-file speaker constraint, and orphan cleanup — using only savepoint-
created rows. The 5 real benchmark files (admin-owned, 600+ real segments) are
never mutated.

Run: ``venv/bin/pytest tests/api/test_transcript_segments.py -v -n0``
"""

from __future__ import annotations

import uuid

from fastapi import status

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import TranscriptSegment


def _make_file(db_session, owner) -> MediaFile:
    mf = MediaFile(
        user_id=owner.id,
        filename="seg_test.wav",
        storage_path=f"test/{uuid.uuid4().hex}.wav",
        file_size=1024,
        content_type="audio/wav",
        status="completed",
    )
    db_session.add(mf)
    db_session.commit()
    db_session.refresh(mf)
    return mf


def _make_segment(db_session, media_file, *, speaker_id=None, text="hello") -> TranscriptSegment:
    seg = TranscriptSegment(
        media_file_id=media_file.id,
        start_time=0.0,
        end_time=1.0,
        text=text,
        speaker_id=speaker_id,
    )
    db_session.add(seg)
    db_session.commit()
    db_session.refresh(seg)
    return seg


def _make_speaker(db_session, owner, media_file, *, name="SPEAKER_00") -> Speaker:
    spk = Speaker(user_id=owner.id, media_file_id=media_file.id, name=name)
    db_session.add(spk)
    db_session.commit()
    db_session.refresh(spk)
    return spk


# ---------------------------------------------------------------------------
# Happy paths: reassign + unassign
# ---------------------------------------------------------------------------


def test_assign_segment_to_speaker_owner_200(client, user_token_headers, normal_user, db_session):
    """The owner assigns a same-file speaker to their segment."""
    mf = _make_file(db_session, normal_user)
    seg = _make_segment(db_session, mf)
    spk = _make_speaker(db_session, normal_user, mf)

    resp = client.put(
        f"/api/transcripts/segments/{seg.uuid}/speaker",
        headers=user_token_headers,
        json={"speaker_uuid": str(spk.uuid)},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.json()
    body = resp.json()
    assert body["uuid"] == str(seg.uuid)
    assert body["speaker_id"] == str(spk.uuid)
    assert body["speaker_label"] == "SPEAKER_00"


def test_unassign_segment_speaker_owner_200(client, user_token_headers, normal_user, db_session):
    """Passing ``speaker_uuid=null`` unassigns the speaker (no orphan crash)."""
    mf = _make_file(db_session, normal_user)
    spk = _make_speaker(db_session, normal_user, mf)
    # Two segments on the speaker so the unassign does NOT orphan-delete it.
    seg = _make_segment(db_session, mf, speaker_id=spk.id)
    _make_segment(db_session, mf, speaker_id=spk.id, text="second")

    resp = client.put(
        f"/api/transcripts/segments/{seg.uuid}/speaker",
        headers=user_token_headers,
        json={"speaker_uuid": None},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.json()
    assert resp.json()["speaker_id"] is None


def test_unassign_orphans_speaker_cleanup(client, user_token_headers, normal_user, db_session):
    """Reassigning the sole segment off a speaker deletes the now-orphaned speaker."""
    mf = _make_file(db_session, normal_user)
    orphan_spk = _make_speaker(db_session, normal_user, mf, name="SPEAKER_ORPHAN")
    keep_spk = _make_speaker(db_session, normal_user, mf, name="SPEAKER_KEEP")
    seg = _make_segment(db_session, mf, speaker_id=orphan_spk.id)
    orphan_id = orphan_spk.id

    resp = client.put(
        f"/api/transcripts/segments/{seg.uuid}/speaker",
        headers=user_token_headers,
        json={"speaker_uuid": str(keep_spk.uuid)},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.json()
    # The orphaned speaker (no remaining segments) is gone.
    assert db_session.query(Speaker).filter(Speaker.id == orphan_id).first() is None


# ---------------------------------------------------------------------------
# Auth / not-found / validation
# ---------------------------------------------------------------------------


def test_update_segment_unauthorized(client, normal_user, db_session):
    mf = _make_file(db_session, normal_user)
    seg = _make_segment(db_session, mf)
    resp = client.put(f"/api/transcripts/segments/{seg.uuid}/speaker", json={"speaker_uuid": None})
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED


def test_update_segment_other_user_403(client, other_user_auth_headers, normal_user, db_session):
    """A non-owner is blocked by the segment-file owner gate (:242)."""
    mf = _make_file(db_session, normal_user)
    seg = _make_segment(db_session, mf)
    resp = client.put(
        f"/api/transcripts/segments/{seg.uuid}/speaker",
        headers=other_user_auth_headers,
        json={"speaker_uuid": None},
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN
    assert resp.json()["detail"] == "Not authorized to modify this transcript segment"


def test_update_segment_admin_bypasses_ownership(
    client, admin_token_headers, normal_user, db_session
):
    """Admin bypasses the segment-file owner gate (require_resource_owner here does
    NOT pass allow_admin — but the segment is reachable; pin the REAL behavior).

    The endpoint calls ``require_resource_owner`` WITHOUT ``allow_admin=True``, so
    an admin who is not the file owner is still blocked with the same 403. This
    snapshots that — the transcript-segment gate is strict ownership, no admin
    bypass — so a future dedup refactor can't silently add one.
    """
    mf = _make_file(db_session, normal_user)
    seg = _make_segment(db_session, mf)
    resp = client.put(
        f"/api/transcripts/segments/{seg.uuid}/speaker",
        headers=admin_token_headers,
        json={"speaker_uuid": None},
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN
    assert resp.json()["detail"] == "Not authorized to modify this transcript segment"


def test_update_segment_nonexistent_404(client, user_token_headers):
    resp = client.put(
        f"/api/transcripts/segments/{uuid.uuid4()}/speaker",
        headers=user_token_headers,
        json={"speaker_uuid": None},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "Transcript segment not found"


def test_update_segment_malformed_uuid_400(client, user_token_headers):
    """``segment_uuid`` is a ``str`` path param; ``get_by_uuid`` rejects a bad UUID
    with 400 'Invalid UUID format: ...' before touching the DB."""
    resp = client.put(
        "/api/transcripts/segments/not-a-uuid/speaker",
        headers=user_token_headers,
        json={"speaker_uuid": None},
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert resp.json()["detail"] == "Invalid UUID format: not-a-uuid"


def test_assign_unknown_speaker_404(client, user_token_headers, normal_user, db_session):
    """A well-formed but unknown speaker UUID → 404 'Speaker not found'."""
    mf = _make_file(db_session, normal_user)
    seg = _make_segment(db_session, mf)
    resp = client.put(
        f"/api/transcripts/segments/{seg.uuid}/speaker",
        headers=user_token_headers,
        json={"speaker_uuid": str(uuid.uuid4())},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "Speaker not found"


def test_assign_speaker_from_other_file_400(client, user_token_headers, normal_user, db_session):
    """A speaker that belongs to a DIFFERENT file is rejected with 400."""
    mf = _make_file(db_session, normal_user)
    seg = _make_segment(db_session, mf)
    other_file = _make_file(db_session, normal_user)
    foreign_spk = _make_speaker(db_session, normal_user, other_file)

    resp = client.put(
        f"/api/transcripts/segments/{seg.uuid}/speaker",
        headers=user_token_headers,
        json={"speaker_uuid": str(foreign_spk.uuid)},
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert resp.json()["detail"] == "Speaker does not belong to the same media file as this segment"


def test_update_segment_extra_field_ignored_200(
    client, user_token_headers, normal_user, db_session
):
    """SegmentSpeakerUpdate only declares ``speaker_uuid``; unknown JSON keys are
    ignored by Pydantic (no 422) — pin that lenient-body contract."""
    mf = _make_file(db_session, normal_user)
    seg = _make_segment(db_session, mf)
    resp = client.put(
        f"/api/transcripts/segments/{seg.uuid}/speaker",
        headers=user_token_headers,
        json={"speaker_uuid": None, "bogus": "ignored"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.json()
