"""GET /search/filters must exclude quarantined files' facets (A2 leak class, #1253).

There is no hit list here, only aggregated buckets, so a takedown has to be excluded inside the
aggregation query itself. ``takedown_service.quarantine_file`` stamps
``availability: quarantined`` on the file's documents (``availability_stamp``, an in-place
``update_by_query``), and the ACL clause every facet query ANDs in excludes it, except under
the review exemption (``is_admin`` = ``file_availability.unavailable_list_exempt``).

Real OpenSearch is required: the thing under test is the stamp landing on the documents and
the query body actually excluding them from ``aggs``, which an in-memory stand-in cannot
prove. The takedown and the stamp open their own sessions; patching
``app.db.session_utils.session_scope`` to the test's ``db_session`` is the same bridge
``test_digest_plane_opensearch.py`` uses, and without it a savepoint-isolated ``MediaFile``
row is invisible to a genuinely separate connection.

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
from tests.fixtures.opensearch_test_index import create_test_index
from tests.fixtures.opensearch_test_index import delete_test_index

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

    The stamp is scoped to this index only: the transcript and speaker targets are pointed at
    names that do not exist, so the takedown never touches a real index.
    """
    from app.core.config import settings
    from app.services.opensearch_service import get_opensearch_client
    from app.services.search import index_admin
    from app.services.search import pipelines

    client = get_opensearch_client()
    assert client is not None, "SKIP_OPENSEARCH said a cluster was reachable but it is not"

    @contextlib.contextmanager
    def _test_session():
        yield db_session

    monkeypatch.setattr("app.db.session_utils.session_scope", _test_session)

    name = f"test_search_filters_quarantine_{uuid_pkg.uuid4().hex[:12]}"
    try:
        create_test_index(client, name, index_admin._get_index_body_with_dimension(384))
        monkeypatch.setattr(settings, "OPENSEARCH_CHUNKS_INDEX", name)
        monkeypatch.setattr(settings, "OPENSEARCH_TRANSCRIPT_INDEX", f"{name}_absent")
        monkeypatch.setattr(settings, "OPENSEARCH_SPEAKER_INDEX", f"{name}_speakers_absent")
        monkeypatch.setattr(settings, "OPENSEARCH_NEURAL_SEARCH_ENABLED", False)
        pipelines.reset_neural_pipeline_state()
        yield client
    finally:
        delete_test_index(client, name)
        pipelines.reset_neural_pipeline_state()


def _user(db_session, *, admin: bool = False) -> User:
    user = User(
        email=f"quarantine_facet_{uuid_pkg.uuid4().hex[:10]}@example.com",
        hashed_password="x",
        is_active=True,
        is_superuser=admin,
        role="super_admin" if admin else "user",
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
        storage_path="",
        file_size=1,
        content_type="audio/wav",
    )
    db_session.add(media_file)
    db_session.flush()
    return media_file


def test_a_quarantined_files_facets_are_excluded_including_for_its_owner(chunk_index, db_session):
    """The fix: speaker/tag facets from a quarantined file vanish for its own owner.

    The takedown goes through the real ``quarantine_file``, so this proves the stamp and
    the reader together; release must bring the facets back.
    """
    from app.core.config import settings
    from app.services.search.hybrid_search_service import HybridSearchService
    from app.services.search.indexing_service import TranscriptIndexingService
    from app.services.takedown_service import TakedownActor
    from app.services.takedown_service import quarantine_file
    from app.services.takedown_service import release_file

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

    admin = _user(db_session, admin=True)
    quarantine_file(
        db_session,
        media_file,
        actor=TakedownActor.platform(admin),
        reason="takedown (test)",
        legal_hold=False,
        operator_action_reason="operator action (test)",
    )

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

    release_file(db_session, media_file, actor=TakedownActor.platform(admin), reason="released")
    released = service.get_available_filters(user_id=user.id, is_admin=False)
    assert distinctive_speaker in [s["name"] for s in released["speakers"]], (
        "a released file's facets did not come back"
    )
