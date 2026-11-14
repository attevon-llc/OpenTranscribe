"""``GET /api/files/{uuid}`` runs the same number of statements however big the file is.

LOADRUN5 saw the route at p95 320 ms against a 300 ms SLO with 30-plus statements per request.
The per-row loads are already one query per relation (``page_transcript_segments`` and
``list_file_speakers_with_profile`` use ``selectinload``, SQLAlchemy 2.0 "Relationship Loading
Techniques"), so what remains is a fixed per-request cost. This pins that it stays fixed: a
change that loads a relation per speaker or per segment shows as a count that grows with the file
(``tests/query_budget.py``: assert equality across two sizes, never a magic ceiling).

``log.describe()`` in the failure message, and ``pytest -s``, list the numbered statements, which
is the census to read when looking for a repeated lookup to merge.
"""

from __future__ import annotations

import uuid as uuid_pkg

from fastapi import status

from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker
from tests.query_budget import count_statements


def _file(db, owner_id: int, speakers: int, segments: int) -> MediaFile:
    media_file = MediaFile(
        user_id=owner_id,
        filename="detail.wav",
        storage_path=f"test/{uuid_pkg.uuid4().hex}.wav",
        file_size=1024,
        content_type="audio/wav",
        status="processing",
    )
    db.add(media_file)
    db.commit()
    rows = [
        Speaker(user_id=owner_id, media_file_id=media_file.id, name=f"SPEAKER_{i:02d}")
        for i in range(speakers)
    ]
    db.add_all(rows)
    db.commit()
    for i in range(segments):
        db.add(
            TranscriptSegment(
                media_file_id=media_file.id,
                speaker_id=rows[i % speakers].id,
                start_time=float(i),
                end_time=float(i) + 1.0,
                text=f"segment {i}",
            )
        )
    db.commit()
    db.refresh(media_file)
    return media_file


def _read(client, headers, db_session, media_file):
    # The first request after the file is created warms per-process caches; measure the second.
    client.get(f"/api/files/{media_file.uuid}", headers=headers)
    with count_statements(db_session.connection()) as log:
        response = client.get(f"/api/files/{media_file.uuid}", headers=headers)
    assert response.status_code == status.HTTP_200_OK, response.text
    return response.json(), log


def test_the_detail_read_statement_count_does_not_grow_with_speakers_or_segments(
    client, user_token_headers, normal_user, db_session
):
    small = _file(db_session, normal_user.id, speakers=2, segments=4)
    large = _file(db_session, normal_user.id, speakers=6, segments=18)

    small_body, small_log = _read(client, user_token_headers, db_session, small)
    large_body, large_log = _read(client, user_token_headers, db_session, large)

    # Control: both responses really carried their rows, so equality is not two empty reads.
    assert len(small_body["speakers"]) == 2 and len(small_body["transcript_segments"]) == 4
    assert len(large_body["speakers"]) == 6 and len(large_body["transcript_segments"]) == 18
    assert large_log.count == small_log.count, (
        f"{small_log.count} statements for the small file, {large_log.count} for the large one:\n"
        f"{large_log.describe()}"
    )
