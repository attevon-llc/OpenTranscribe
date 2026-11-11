"""A taken-down file leaves search, chat retrieval and speaker matching, and comes back (#1253).

The quarantine twin of ``test_trash_index_lifecycle_opensearch.py``, against a real OpenSearch
and a real database, because the thing under test is two systems agreeing:
``takedown_service.quarantine_file`` commits the row, then rewrites the file's documents in place
(``availability_stamp``: ``update_by_query`` with ``refresh=True``) to ``quarantined``, and every
reader excludes it inside the query itself, except under the quarantine review exemption.

The readers are called at the SERVICE level, below the Postgres post-filter
(``file_availability.unavailable_among``): with the post-filter in front, a broken index filter
would still pass. Every test creates its own users, files and throwaway indices (random names,
deleted in teardown) and never touches dev data or the real ``transcript_chunks`` /
``transcripts`` / ``speakers*`` indices. Point it at an isolated stack, never the shared dev one::

    OPENSEARCH_PORT=5280 POSTGRES_PORT=5276 \\
        pytest backend/tests/integration/test_quarantine_index_lifecycle_opensearch.py -m integration
"""

from __future__ import annotations

import os
import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from sqlalchemy import insert

from app.models.media import MediaFile
from app.models.user import User
from app.services import trash_service
from app.services.takedown_service import TakedownActor
from app.services.takedown_service import quarantine_file
from app.services.takedown_service import release_file
from tests.fixtures.availability_index_world import PHRASE
from tests.fixtures.availability_index_world import SPEAKER
from tests.fixtures.availability_index_world import docs_of
from tests.fixtures.availability_index_world import index_file
from tests.fixtures.availability_index_world import make_media_file
from tests.fixtures.availability_index_world import make_user
from tests.fixtures.availability_index_world import visible_surfaces

_OPENSEARCH_ABSENT = os.environ.get("SKIP_OPENSEARCH", "True").lower() == "true"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.xdist_group("opensearch_speaker_indices"),
    pytest.mark.skipif(
        _OPENSEARCH_ABSENT,
        reason=(
            "No OpenSearch reachable (SKIP_OPENSEARCH). Start an isolated stack and export "
            "OPENSEARCH_PORT: a stand-in index cannot prove that update_by_query stamps the "
            "documents or that the must_not excludes them from hits, counts and kNN."
        ),
    ),
]


def _admin(db_session) -> User:
    admin = make_user(db_session, prefix="quarantine_admin")
    admin.role = "super_admin"
    admin.is_superuser = True
    db_session.commit()
    return admin


def _take_down(db_session, media_file: MediaFile, admin: User) -> None:
    quarantine_file(
        db_session,
        media_file,
        actor=TakedownActor.platform(admin),
        reason="takedown (test)",
        legal_hold=False,
        operator_action_reason="operator action (test)",
    )


def _release(db_session, media_file: MediaFile, admin: User) -> None:
    release_file(db_session, media_file, actor=TakedownActor.platform(admin), reason="released")


def test_a_quarantined_file_leaves_every_index_read_surface_and_release_brings_it_back(
    availability_indices,
    db_session,
):
    user, admin = make_user(db_session), _admin(db_session)
    media_file = make_media_file(db_session, user)
    index_file(availability_indices, db_session, media_file)

    before = visible_surfaces(user, media_file)
    assert all(before.values()), f"control: the file must be visible before the takedown: {before}"

    _take_down(db_session, media_file, admin)
    taken_down = visible_surfaces(user, media_file)
    assert not any(taken_down.values()), f"a quarantined file is still visible on: {taken_down}"

    _release(db_session, media_file, admin)
    after = visible_surfaces(user, media_file)
    assert all(after.values()), f"a released file did not come back on: {after}"


def test_the_review_exemption_still_sees_a_quarantined_file(
    availability_indices,
    db_session,
):
    from app.services.search.hybrid_search_service import HybridSearchService

    user, admin = make_user(db_session), _admin(db_session)
    media_file = make_media_file(db_session, user)
    index_file(availability_indices, db_session, media_file)
    _take_down(db_session, media_file, admin)

    service = HybridSearchService()
    hits = service.search(
        query=PHRASE, user_id=user.id, search_mode="keyword", include_quarantined=True
    )
    facets = service.get_available_filters(user_id=user.id, is_admin=True)

    assert any(h.file_uuid == str(media_file.uuid) for h in hits.results)
    assert service.count_matches(query=PHRASE, user_id=user.id, is_admin=True) > 0
    assert SPEAKER in [s["name"] for s in facets["speakers"]]


def test_the_stamp_reaches_every_plane_and_a_trashed_takedown_reads_trashed(
    availability_indices,
    db_session,
):
    from app.core.config import settings

    user, admin = make_user(db_session), _admin(db_session)
    media_file = make_media_file(db_session, user)
    index_file(availability_indices, db_session, media_file)

    _take_down(db_session, media_file, admin)
    docs = docs_of(availability_indices, settings.OPENSEARCH_CHUNKS_INDEX, media_file)
    assert {"chunk", "digest", "summary"} <= {d["doc_type"] for d in docs}
    assert {d["availability"] for d in docs} == {"quarantined"}
    transcript = availability_indices.get(
        index=settings.OPENSEARCH_TRANSCRIPT_INDEX, id=str(media_file.uuid)
    )
    assert transcript["_source"]["availability"] == "quarantined"

    trash_service.trash_files(db_session, [media_file], actor=user, request=None, bypass=None)
    assert {
        d["availability"]
        for d in docs_of(availability_indices, settings.OPENSEARCH_CHUNKS_INDEX, media_file)
    } == {"trashed"}, "a file both trashed and quarantined must read trashed"


def test_a_quarantined_files_voice_is_not_matched_and_comes_back_on_release(
    availability_indices,
    db_session,
):
    from app.core.embedding_models import PYANNOTE_EMBEDDING_DIMENSION_V4
    from app.core.embedding_models import get_speaker_index_v4
    from app.services.opensearch_service.matching import find_matching_speaker

    user, admin = make_user(db_session), _admin(db_session)
    media_file = make_media_file(db_session, user)
    vector = [((i % 5) + 1) / 10.0 for i in range(PYANNOTE_EMBEDDING_DIMENSION_V4)]
    # Written without an availability value: a voiceprint from before the takedown field.
    availability_indices.index(
        index=get_speaker_index_v4(),
        id=str(uuid_pkg.uuid4()),
        refresh=True,
        body={
            "speaker_id": 999_101,
            "speaker_uuid": str(uuid_pkg.uuid4()),
            "user_id": user.id,
            "name": SPEAKER,
            "collection_ids": [],
            "media_file_id": media_file.id,
            "segment_count": 1,
            "embedding": vector,
        },
    )

    def _match():
        return find_matching_speaker(vector, user.id, threshold=0.5, organization_id=None)

    assert _match() is not None, "control: the voice must match before the takedown"
    _take_down(db_session, media_file, admin)
    assert _match() is None, "a quarantined file's voice still matched"
    _release(db_session, media_file, admin)
    assert _match() is not None, "a released file's voice did not come back"


def test_the_boot_reconcile_stamps_a_takedown_that_predates_the_field(
    availability_indices,
    db_session,
):
    """A file taken down before the index carried quarantine has documents with no value; the
    unscoped reconcile (beat and once per boot) stamps it."""
    from app.core.config import settings
    from app.tasks import search_availability_task

    user = make_user(db_session)
    media_file = make_media_file(db_session, user)
    index_file(availability_indices, db_session, media_file)
    media_file.is_quarantined = True  # the pre-upgrade shape: a flag with no stamp
    db_session.commit()
    assert {
        d.get("availability")
        for d in docs_of(availability_indices, settings.OPENSEARCH_CHUNKS_INDEX, media_file)
    } == {"available"}

    search_availability_task.availability_reconcile.run()

    docs = docs_of(availability_indices, settings.OPENSEARCH_CHUNKS_INDEX, media_file)
    assert {d["availability"] for d in docs} == {"quarantined"}


def test_search_still_answers_with_10001_quarantined_files(
    availability_indices,
    db_session,
    client,
    normal_user,
    user_token_headers,
):
    """Past 10,000 taken-down files the old Postgres-resolved list failed closed with a 503 on
    facets, suggestions and counts. The quarantine is a field now, so they answer 200."""
    live = make_media_file(db_session, normal_user)
    index_file(availability_indices, db_session, live)
    now = datetime.now(UTC) - timedelta(minutes=1)
    db_session.execute(
        insert(MediaFile),
        [
            {
                "uuid": uuid_pkg.uuid4(),
                "user_id": normal_user.id,
                "filename": f"q-{i}.wav",
                "storage_path": f"x/q-{i}.wav",
                "file_size": 1,
                "content_type": "audio/wav",
                "is_quarantined": True,
                "quarantined_at": now,
            }
            for i in range(10_001)
        ],
    )
    db_session.commit()

    for path, params in (
        ("/api/search/filters", {}),
        ("/api/search/suggestions", {"q": "Budget"}),
        ("/api/search/count", {"q": PHRASE}),
    ):
        response = client.get(path, headers=user_token_headers, params=params)
        assert response.status_code == 200, (path, response.text)
