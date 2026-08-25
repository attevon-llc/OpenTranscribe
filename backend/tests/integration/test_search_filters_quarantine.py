"""GET /search/filters must exclude quarantined files' facets (A2 leak class).

``takedown_service.quarantine_file`` writes only Postgres — no OpenSearch call
exists anywhere in ``takedown_service.py`` — so before this fix a quarantined
file's speaker names, tag names and upload-time date range kept appearing in
this endpoint's facet aggregation for anyone who had access before the
takedown, including the file's own owner (whom ``takedown_service.is_hidden_for``
says must be treated as if the file no longer exists).

``search.py``'s sibling ``search_summaries``/``_drop_quarantined_search_hits``
already had the right shape for a *hit list* — resolve the quarantined uuids in
Postgres, drop them from the OpenSearch response. There is no hit list here,
only aggregated buckets, so the fix has to build the exclusion into the
aggregation query itself
(``HybridSearchService.get_available_filters``'s ``_quarantined_file_uuids()``
+ a ``must_not`` clause) rather than post-filtering a response.

Real OpenSearch is required: the thing under test is the query body actually
excluding matching documents from `aggs`, which an in-memory stand-in cannot
prove. ``_index_digest_plane``/``_quarantined_file_uuids`` open their own
session — patching ``app.db.session_utils.session_scope`` to the test's
``db_session`` is the same bridge ``test_digest_plane_opensearch.py`` and
``test_chat_endpoints.py`` use, and without it a savepoint-isolated
``MediaFile`` row is invisible to a genuinely separate connection.

Point it at an isolated stack, never the shared dev one::

    OPENSEARCH_PORT=5280 POSTGRES_PORT=5276 \\
        pytest backend/tests/integration/test_search_filters_quarantine.py -m integration
"""

from __future__ import annotations

import contextlib
import os
import uuid as uuid_pkg

import pytest

from app.models.media import MediaFile
from app.models.user import User

_OPENSEARCH_ABSENT = os.environ.get("SKIP_OPENSEARCH", "True").lower() == "true"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        _OPENSEARCH_ABSENT,
        reason=(
            "No OpenSearch reachable (SKIP_OPENSEARCH). Start an isolated stack and export "
            "OPENSEARCH_PORT — a stand-in index cannot validate that the aggregation query "
            "itself excludes the quarantined documents."
        ),
    ),
]


@pytest.fixture
def chunk_index(monkeypatch, db_session):
    """A throwaway chunks index with the REAL mapping, and ``session_scope`` bridged.

    ``HybridSearchService._quarantined_file_uuids`` opens its own session — under the
    savepoint harness a genuinely separate connection cannot see this test's
    uncommitted ``MediaFile`` row, so without the bridge the quarantine exclusion
    would silently resolve to an empty set and the fix's own test would pass
    vacuously (exactly the class of bug ``test_digest_plane_opensearch.py``'s
    docstring warns about).
    """
    from app.core.config import settings
    from app.services.opensearch_service import get_opensearch_client
    from app.services.search import indexing_service as svc

    client = get_opensearch_client()
    assert client is not None, "SKIP_OPENSEARCH said a cluster was reachable but it is not"

    @contextlib.contextmanager
    def _test_session():
        yield db_session

    monkeypatch.setattr("app.db.session_utils.session_scope", _test_session)

    name = f"test_search_filters_quarantine_{uuid_pkg.uuid4().hex[:12]}"
    client.indices.create(index=name, body=svc._get_index_body_with_dimension(384))
    monkeypatch.setattr(settings, "OPENSEARCH_CHUNKS_INDEX", name)
    monkeypatch.setattr(settings, "OPENSEARCH_NEURAL_SEARCH_ENABLED", False)
    svc.reset_neural_pipeline_state()
    try:
        yield client
    finally:
        client.indices.delete(index=name, ignore=[404])
        svc.reset_neural_pipeline_state()


def _user(db_session) -> User:
    user = User(
        email=f"quarantine_facet_{uuid_pkg.uuid4().hex[:10]}@example.com",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        role="user",
        auth_type="local",
    )
    db_session.add(user)
    db_session.flush()
    return user


def _media_file(db_session, user: User) -> MediaFile:
    media_file = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename="quarantine-facet.wav",
        storage_path=f"x/{uuid_pkg.uuid4().hex}.wav",
        file_size=1,
        content_type="audio/wav",
    )
    db_session.add(media_file)
    db_session.flush()
    return media_file


def test_a_quarantined_files_facets_are_excluded_including_for_its_owner(chunk_index, db_session):
    """The fix: speaker/tag facets from a quarantined file vanish for its own owner.

    Watched red against ``git archive HEAD``: the pre-fix ``get_available_filters``
    filters only on ``accessible_user_ids`` + org, with no quarantine predicate at
    all, so the distinctive speaker/tag survive the takedown for this test's owner.
    """
    from app.core.config import settings
    from app.services.search.hybrid_search_service import HybridSearchService
    from app.services.search.indexing_service import TranscriptIndexingService

    user = _user(db_session)
    media_file = _media_file(db_session, user)
    file_uuid = str(media_file.uuid)

    distinctive_speaker = f"Zzyzx-Speaker-{uuid_pkg.uuid4().hex[:8]}"
    distinctive_tag = f"zzyzx-tag-{uuid_pkg.uuid4().hex[:8]}"

    segments = [
        {
            "start": 0.0,
            "end": 5.0,
            "text": "A distinctive statement about the quarterly budget review.",
            "speaker": distinctive_speaker,
        }
    ]

    result = TranscriptIndexingService().index_transcript_chunks(
        file_id=media_file.id,
        file_uuid=file_uuid,
        user_id=user.id,
        segments=segments,
        title="Quarantine facet leak check",
        speakers=[distinctive_speaker],
        tags=[distinctive_tag],
        accessible_user_ids=[user.id],
    )
    assert isinstance(result, dict) and result.get("chunk_count", 0) >= 1, (
        f"setup did not index a chunk to test against: {result!r}"
    )
    chunk_index.indices.refresh(index=settings.OPENSEARCH_CHUNKS_INDEX)

    service = HybridSearchService()

    # Before the takedown: the owner's own facets are there (sanity control — the
    # exclusion below must be provably about quarantine, not about the query
    # never finding this document at all).
    before = service.get_available_filters(user_id=user.id, is_admin=False)
    assert distinctive_speaker in [s["name"] for s in before["speakers"]]
    assert distinctive_tag in [t["name"] for t in before["tags"]]

    # Quarantine exactly the way takedown_service.quarantine_file does it:
    # Postgres only, no OpenSearch write.
    media_file.is_quarantined = True
    db_session.commit()

    after = service.get_available_filters(user_id=user.id, is_admin=False)
    after_speakers = [s["name"] for s in after["speakers"]]
    after_tags = [t["name"] for t in after["tags"]]
    assert distinctive_speaker not in after_speakers, (
        "a quarantined file's speaker facet leaked to its own owner"
    )
    assert distinctive_tag not in after_tags, (
        "a quarantined file's tag facet leaked to its own owner"
    )

    # Admins keep review visibility, matching search.py's _drop_quarantined_search_hits
    # bypass on the results page beside this endpoint.
    admin_view = service.get_available_filters(user_id=user.id, is_admin=True)
    admin_speakers = [s["name"] for s in admin_view["speakers"]]
    assert distinctive_speaker in admin_speakers, (
        "an admin lost review visibility into a quarantined file's facets"
    )
