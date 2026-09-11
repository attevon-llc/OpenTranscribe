"""``PUT /api/transcripts/segments/{uuid}/speaker`` must gate on quarantine too (#817).

This route resolves its file with a raw ``db.query(MediaFile)...first()`` (never
``get_file_by_uuid_with_permission``, the chokepoint that makes a taken-down file 404
on every ``files/`` read surface), then checks ownership and writes the new speaker
assignment. Without a quarantine check, a taken-down file's OWNER could still
reassign a segment's speaker on it — mutating a recording an admin has placed under
review, and dispatching a reindex that would put the change into the search index
too.

404, not 403, matching every other takedown surface — "this exists but you can't see
it" is itself a disclosure about a taken-down file.
"""

from __future__ import annotations

import uuid

from fastapi import status

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import TranscriptSegment


def _make_file_speaker_segment(
    db_session, owner, **file_overrides
) -> tuple[MediaFile, Speaker, TranscriptSegment]:
    file_uuid = str(uuid.uuid4())
    defaults = {
        "uuid": file_uuid,
        "filename": "segment_quarantine.wav",
        "title": "segment_quarantine",
        "storage_path": f"media/test/{file_uuid}.wav",
        "content_type": "audio/wav",
        "file_size": 4096,
        "status": "completed",
        "is_public": False,
        "user_id": owner.id,
    }
    defaults.update(file_overrides)
    media_file = MediaFile(**defaults)
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)

    speaker = Speaker(
        uuid=uuid.uuid4(),
        user_id=owner.id,
        media_file_id=media_file.id,
        name="SPEAKER_00",
        display_name="Alex",
    )
    db_session.add(speaker)
    db_session.commit()
    db_session.refresh(speaker)

    segment = TranscriptSegment(
        uuid=uuid.uuid4(),
        media_file_id=media_file.id,
        speaker_id=None,
        start_time=0.0,
        end_time=1.0,
        text="a segment nobody should be able to relabel while quarantined",
    )
    db_session.add(segment)
    db_session.commit()
    db_session.refresh(segment)

    return media_file, speaker, segment


def test_speaker_update_is_refused_for_a_quarantined_file(
    client, user_token_headers, normal_user, db_session, monkeypatch
):
    """The finding: a takedown must block segment mutation too, not just reads."""
    dispatched = []
    monkeypatch.setattr(
        "app.services.search.reindex_dispatch.dispatch_transcript_reindex",
        lambda **kwargs: dispatched.append(kwargs),
    )

    _media_file, speaker, segment = _make_file_speaker_segment(
        db_session, normal_user, is_quarantined=True
    )

    response = client.put(
        f"/api/transcripts/segments/{segment.uuid}/speaker",
        json={"speaker_uuid": str(speaker.uuid)},
        headers=user_token_headers,
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert "segment nobody should be able to relabel" not in response.text
    assert dispatched == []


def test_speaker_update_does_not_write_when_refused(
    client, user_token_headers, normal_user, db_session, monkeypatch
):
    """The refusal must be enforced before the write, not merely on the response."""
    monkeypatch.setattr(
        "app.services.search.reindex_dispatch.dispatch_transcript_reindex",
        lambda **kwargs: None,
    )

    _media_file, speaker, segment = _make_file_speaker_segment(
        db_session, normal_user, is_quarantined=True
    )

    response = client.put(
        f"/api/transcripts/segments/{segment.uuid}/speaker",
        json={"speaker_uuid": str(speaker.uuid)},
        headers=user_token_headers,
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text

    db_session.expire_all()
    reloaded = db_session.query(TranscriptSegment).filter(TranscriptSegment.id == segment.id).one()
    assert reloaded.speaker_id is None


def test_speaker_update_does_not_dispatch_a_reindex_when_refused(
    client, user_token_headers, normal_user, db_session, monkeypatch
):
    """A refused mutation must not have any of its side effects run either."""
    called = []
    monkeypatch.setattr(
        "app.services.search.reindex_dispatch.dispatch_transcript_reindex",
        lambda **kwargs: called.append(kwargs),
    )

    _media_file, speaker, segment = _make_file_speaker_segment(
        db_session, normal_user, is_quarantined=True
    )

    response = client.put(
        f"/api/transcripts/segments/{segment.uuid}/speaker",
        json={"speaker_uuid": str(speaker.uuid)},
        headers=user_token_headers,
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert called == []


def test_speaker_update_still_works_for_a_file_that_is_not_quarantined(
    client, user_token_headers, normal_user, db_session, monkeypatch
):
    """The control, without which the refusal tests would pass on a broken route."""
    monkeypatch.setattr(
        "app.services.search.reindex_dispatch.dispatch_transcript_reindex",
        lambda **kwargs: None,
    )

    _media_file, speaker, segment = _make_file_speaker_segment(db_session, normal_user)

    response = client.put(
        f"/api/transcripts/segments/{segment.uuid}/speaker",
        json={"speaker_uuid": str(speaker.uuid)},
        headers=user_token_headers,
    )
    assert response.status_code == status.HTTP_200_OK, response.text

    db_session.expire_all()
    reloaded = db_session.query(TranscriptSegment).filter(TranscriptSegment.id == segment.id).one()
    assert reloaded.speaker_id == speaker.id


def test_an_admin_can_still_update_a_quarantined_files_segment(
    client, admin_token_headers, admin_user, db_session, monkeypatch
):
    """An admin who cannot reach a quarantined file's segments cannot fix a
    mis-diarized speaker before releasing it — the same bypass every other
    takedown surface grants."""
    monkeypatch.setattr(
        "app.services.search.reindex_dispatch.dispatch_transcript_reindex",
        lambda **kwargs: None,
    )

    _media_file, speaker, segment = _make_file_speaker_segment(
        db_session, admin_user, is_quarantined=True
    )

    response = client.put(
        f"/api/transcripts/segments/{segment.uuid}/speaker",
        json={"speaker_uuid": str(speaker.uuid)},
        headers=admin_token_headers,
    )
    assert response.status_code == status.HTTP_200_OK, response.text
