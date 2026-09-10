"""Issue #890 — editing a segment's text never refreshed the file's stored analytics.

``AnalyticsService.refresh_analytics`` was already called from the speaker-reassignment
and speaker-merge paths (``transcript_segments.py``, ``speakers.py``), and the text-edit
path already dispatches an OpenSearch reindex for the same reason (issue #666) — but it
never called ``refresh_analytics``. ``_get_or_compute_analytics`` only computes when the
stored row is ABSENT (issue #272's fix, pinned by
``test_issue272_status_comparisons.py::test_existing_analytics_short_circuits``), so an
existing row is returned unchanged after a text edit: the UI kept rendering pre-edit word
counts with no error.

These are API-level tests against a real Postgres session (``db_session``/``client``
fixtures) rather than a mocked one, because the bug is specifically about a STORED value
staying stale — a mock would have to fake the very staleness being tested.
"""

from __future__ import annotations

import uuid

from fastapi import status

from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.models.media import TranscriptSegment


def _make_file_with_segment(db_session, owner, *, text: str) -> tuple[MediaFile, TranscriptSegment]:
    file_uuid = str(uuid.uuid4())
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename=f"analytics890_{uuid.uuid4().hex[:8]}.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=4096,
        status=FileStatus.COMPLETED,
        duration=30.0,
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)

    segment = TranscriptSegment(
        media_file_id=media_file.id,
        start_time=0.0,
        end_time=5.0,
        text=text,
    )
    db_session.add(segment)
    db_session.commit()
    db_session.refresh(segment)
    return media_file, segment


def test_editing_segment_text_updates_the_word_count(
    client, user_token_headers, normal_user, db_session
):
    media_file, segment = _make_file_with_segment(db_session, normal_user, text="one two three")

    # This first GET is what materialises the analytics row (on-demand compute,
    # issue #272) — asserting it exists BEFORE the edit is what makes the second
    # GET's comparison meaningful. Without it, a fixture with no analytics row
    # yet would let _get_or_compute_analytics compute fresh on the second GET
    # too, and the test would pass whether or not the edit-path fix exists —
    # the exact "assertion that passed against an empty index" shape the repo's
    # own test-quality auditors watch for.
    before = client.get(
        f"/api/files/{media_file.uuid}/analytics", headers=user_token_headers
    ).json()["analytics"]
    assert before is not None, "the on-demand compute must have materialised a row"
    assert before["overall_analytics"]["word_count"] == 3

    edit_response = client.put(
        f"/api/files/{media_file.uuid}/transcript/segments/{segment.uuid}",
        headers=user_token_headers,
        json={"text": "one two three four five six seven eight"},
    )
    assert edit_response.status_code == status.HTTP_200_OK

    after = client.get(
        f"/api/files/{media_file.uuid}/analytics", headers=user_token_headers
    ).json()["analytics"]
    assert after is not None
    assert after["overall_analytics"] != before["overall_analytics"]
    assert after["overall_analytics"]["word_count"] == 8


def test_the_reindex_dispatch_from_666_still_fires_alongside_the_analytics_refresh(
    client, user_token_headers, normal_user, db_session, monkeypatch
):
    """Guards #666's fix from regressing while adding #890's — the two effects
    live in the same `if text_changed:` block and must both still run."""
    calls: list[dict] = []

    def _fake_dispatch(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(
        "app.services.search.reindex_dispatch.dispatch_transcript_reindex", _fake_dispatch
    )

    media_file, segment = _make_file_with_segment(db_session, normal_user, text="hello world")

    response = client.put(
        f"/api/files/{media_file.uuid}/transcript/segments/{segment.uuid}",
        headers=user_token_headers,
        json={"text": "goodbye world"},
    )
    assert response.status_code == status.HTTP_200_OK
    assert calls, "text_changed must still dispatch a reindex (issue #666)"
    assert calls[0]["file_uuid"] == str(media_file.uuid)


def test_a_non_text_edit_does_not_refresh_analytics(
    client, user_token_headers, normal_user, db_session
):
    """``text_changed`` is the gate for BOTH the reindex dispatch and the analytics
    refresh — a start_time-only edit must not recompute analytics."""
    media_file, segment = _make_file_with_segment(db_session, normal_user, text="one two three")

    before = client.get(
        f"/api/files/{media_file.uuid}/analytics", headers=user_token_headers
    ).json()["analytics"]
    assert before is not None

    response = client.put(
        f"/api/files/{media_file.uuid}/transcript/segments/{segment.uuid}",
        headers=user_token_headers,
        json={"start_time": 1.5},
    )
    assert response.status_code == status.HTTP_200_OK

    after = client.get(
        f"/api/files/{media_file.uuid}/analytics", headers=user_token_headers
    ).json()["analytics"]
    assert after["overall_analytics"] == before["overall_analytics"]
