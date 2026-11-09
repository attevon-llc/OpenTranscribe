"""The index-backed post-filters drop a trashed file even for a quarantine reviewer (#1101).

Search hits and persisted chat citations come from OpenSearch / stored payloads, so a
Postgres post-filter (``file_availability.unavailable_among``) removes unavailable files.
Both filters used to be skipped entirely for a caller with the quarantine list exemption
(a single-tenant admin), which was right while quarantine was the only reason to hide a file.
The trash has no review exemption: a trashed file's hits and citations must go for everyone,
while a quarantined file stays visible to the exempt reviewer (the controls below).
"""

from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace

from app.api.endpoints.chat.citation_takedown import drop_quarantined_citations_bulk
from app.api.endpoints.search_helpers import _drop_quarantined_search_hits
from app.models.media import MediaFile


def _file(db_session, owner, *, trashed: bool = False, quarantined: bool = False) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    now = datetime.now(UTC)
    row = MediaFile(
        uuid=file_uuid,
        filename="post_filter.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=4096,
        status="completed",
        user_id=owner.id,
        is_quarantined=quarantined,
        deleted_at=now if trashed else None,
        purge_after=now + timedelta(days=30) if trashed else None,
        trash_origin="user" if trashed else None,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _response(*files: MediaFile) -> SimpleNamespace:
    hits = [SimpleNamespace(file_uuid=str(f.uuid)) for f in files]
    return SimpleNamespace(results=hits, total_results=len(hits), total_files=len(hits))


def test_search_hits_drop_a_trashed_file_for_an_exempt_reviewer(db_session, normal_user):
    live = _file(db_session, normal_user)
    quarantined = _file(db_session, normal_user, quarantined=True)
    trashed = _file(db_session, normal_user, trashed=True)

    kept = _drop_quarantined_search_hits(
        db_session, _response(live, quarantined, trashed), include_quarantined=True
    )

    assert [h.file_uuid for h in kept.results] == [str(live.uuid), str(quarantined.uuid)]
    assert kept.total_results == 2


def test_search_hits_drop_both_for_a_caller_without_the_exemption(db_session, normal_user):
    live = _file(db_session, normal_user)
    quarantined = _file(db_session, normal_user, quarantined=True)
    trashed = _file(db_session, normal_user, trashed=True)

    kept = _drop_quarantined_search_hits(
        db_session, _response(live, quarantined, trashed), include_quarantined=False
    )

    assert [h.file_uuid for h in kept.results] == [str(live.uuid)]


def test_citations_drop_a_trashed_file_for_an_exempt_reviewer(db_session, normal_user):
    live = _file(db_session, normal_user)
    quarantined = _file(db_session, normal_user, quarantined=True)
    trashed = _file(db_session, normal_user, trashed=True)
    citations = [{"file_uuid": str(f.uuid)} for f in (live, quarantined, trashed)]

    bulk = drop_quarantined_citations_bulk(db_session, [citations], include_quarantined=True)

    expected = [{"file_uuid": str(live.uuid)}, {"file_uuid": str(quarantined.uuid)}]
    assert bulk == [expected]
    assert drop_quarantined_citations_bulk(db_session, [citations], include_quarantined=False) == [
        [{"file_uuid": str(live.uuid)}]
    ]
