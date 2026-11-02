"""Speaker clustering when the voiceprint index cannot answer.

Clustering decides on two OpenSearch reads: "does this speaker have a voiceprint?" and
"which cluster centroids is it close to?". Both used to answer an outage with their empty
value, and both empties have consequences: no voiceprint skips the speaker (and drops it
from a centroid rebuild), no nearby centroid creates a NEW cluster for a voice that may
already have one. These tests run the real lookups against a stand-in client that fails,
and check each consequence is withheld; every outage case has a control where the index
answers "nothing" and the normal empty-handed path still runs.
"""

from __future__ import annotations

from typing import Any

import pytest
from opensearchpy.exceptions import ConnectionError as OpenSearchConnectionError
from opensearchpy.exceptions import NotFoundError

from app.models.speaker import Speaker
from app.models.speaker import SpeakerCluster
from app.services.clustering.centroids import CentroidRecomputeError
from app.services.opensearch_service.client import OpenSearchUnavailableError
from app.services.speaker_clustering_service import SpeakerClusteringService
from tests.fixtures.privacy_switches import file_in
from tests.fixtures.privacy_switches import make_org
from tests.fixtures.speaker_cleanup_seams import make_cluster
from tests.fixtures.speaker_cleanup_seams import make_speaker

VOICE = [1.0, 0.0, 0.0, 0.0]
OTHER = [0.0, 1.0, 0.0, 0.0]


def _refused() -> OpenSearchConnectionError:
    return OpenSearchConnectionError("N/A", "connection refused", Exception("refused"))


class _SpeakerIndex:
    """Answers voiceprint ``get`` and centroid ``search`` the way the test chooses."""

    def __init__(self) -> None:
        self.voiceprints: dict[str, list[float]] = {}
        self.unreadable: set[str] = set()
        self.search_error: Exception | None = None

    def get(self, **kw: Any) -> dict[str, Any]:
        doc_id = kw["id"]
        if doc_id in self.unreadable:
            raise _refused()
        if doc_id not in self.voiceprints:
            raise NotFoundError(404, "not_found", {"index": kw["index"]})
        return {"_source": {"embedding": self.voiceprints[doc_id]}}

    def search(self, **_: Any) -> dict[str, Any]:
        if self.search_error is not None:
            raise self.search_error
        return {"hits": {"hits": []}}


@pytest.fixture
def index(speaker_os_seams, monkeypatch) -> _SpeakerIndex:
    """The real voiceprint and centroid lookups, reading a stand-in client.

    ``speaker_os_seams`` still records centroid writes; only its two read stand-ins are
    put back to the production functions, so the code under test is what turns a client
    error into an answer.
    """
    from app.services.opensearch_service import clusters
    from app.services.opensearch_service import speaker_read

    fake = _SpeakerIndex()
    monkeypatch.setattr("app.services.opensearch_service.client.opensearch_client", fake)
    monkeypatch.setattr(
        "app.services.opensearch_service.get_speaker_embedding", speaker_read.get_speaker_embedding
    )
    monkeypatch.setattr(
        "app.services.opensearch_service.find_matching_clusters", clusters.find_matching_clusters
    )
    monkeypatch.setattr(speaker_read, "ensure_indices_exist", lambda: None)
    monkeypatch.setattr(speaker_read, "get_active_speaker_index", lambda: "speakers")
    monkeypatch.setattr(clusters, "get_active_speaker_index", lambda: "speakers")
    monkeypatch.setattr(clusters, "query_embedding_dimension_mismatch", lambda *_: False)
    return fake


@pytest.fixture
def production_rollback(db_session, monkeypatch) -> None:
    """Make ``db.rollback()`` discard only uncommitted work, as it does in production.

    The test session nests every commit in a savepoint of one outer transaction, so a plain
    rollback would also discard the rows this test committed as its setup (the user
    included), and "nothing was kept" would hold trivially.
    """

    def rollback() -> None:
        nested = db_session.get_nested_transaction()
        if nested is not None:
            nested.rollback()

    monkeypatch.setattr(db_session, "rollback", rollback)


def _speaker(db, user, org, index: _SpeakerIndex, voice: list[float] | None) -> Any:
    sp = make_speaker(db, user, file_in(db, user, org))
    if voice is not None:
        index.voiceprints[str(sp.uuid)] = voice
    return sp


def _clusters_of(db, user) -> int:
    return int(db.query(SpeakerCluster).filter(SpeakerCluster.user_id == user.id).count())


# ---------------------------------------------------------------------------
# find_or_create_cluster: the centroid kNN
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("production_rollback")
def test_a_centroid_search_outage_creates_no_cluster(db_session, normal_user, index):
    org = make_org(db_session)
    sp = _speaker(db_session, normal_user, org, index, VOICE)
    db_session.commit()
    index.search_error = _refused()

    with pytest.raises(OpenSearchUnavailableError):
        SpeakerClusteringService(db_session).find_or_create_cluster(sp.id, normal_user.id, VOICE)

    db_session.expire_all()
    assert _clusters_of(db_session, normal_user) == 0
    assert db_session.get(Speaker, sp.id).cluster_id is None


def test_an_empty_centroid_search_still_creates_the_singleton(
    db_session, normal_user, index, speaker_os_seams
):
    org = make_org(db_session)
    sp = _speaker(db_session, normal_user, org, index, VOICE)
    db_session.commit()

    cluster = SpeakerClusteringService(db_session).find_or_create_cluster(
        sp.id, normal_user.id, VOICE
    )

    assert cluster is not None
    assert _clusters_of(db_session, normal_user) == 1
    assert list(speaker_os_seams["clusters"]) == [str(cluster.uuid)]


@pytest.mark.usefixtures("production_rollback")
def test_a_new_cluster_whose_centroid_was_not_stored_is_not_kept(
    db_session, normal_user, index, speaker_os_seams
):
    """A cluster with no centroid can never be found again, so the next file makes another."""
    org = make_org(db_session)
    sp = _speaker(db_session, normal_user, org, index, VOICE)
    db_session.commit()
    speaker_os_seams["store_ok"] = False

    with pytest.raises(OpenSearchUnavailableError):
        SpeakerClusteringService(db_session).find_or_create_cluster(sp.id, normal_user.id, VOICE)

    db_session.expire_all()
    assert _clusters_of(db_session, normal_user) == 0
    assert db_session.get(Speaker, sp.id).cluster_id is None


# ---------------------------------------------------------------------------
# cluster_speakers_for_file: the voiceprint lookup
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("production_rollback")
def test_a_voiceprint_lookup_outage_fails_the_whole_file(db_session, normal_user, index):
    """The first speaker was clustered before the second lookup failed; nothing is kept."""
    org = make_org(db_session)
    media_file = file_in(db_session, normal_user, org)
    first = make_speaker(db_session, normal_user, media_file, name="SPEAKER_00")
    second = make_speaker(db_session, normal_user, media_file, name="SPEAKER_01")
    index.voiceprints[str(first.uuid)] = VOICE
    index.unreadable.add(str(second.uuid))
    db_session.commit()

    with pytest.raises(OpenSearchUnavailableError):
        SpeakerClusteringService(db_session).cluster_speakers_for_file(
            media_file.id, normal_user.id
        )

    db_session.expire_all()
    assert _clusters_of(db_session, normal_user) == 0
    assert db_session.get(Speaker, first.id).cluster_id is None


def test_a_speaker_with_no_voiceprint_is_still_skipped(db_session, normal_user, index):
    org = make_org(db_session)
    media_file = file_in(db_session, normal_user, org)
    first = make_speaker(db_session, normal_user, media_file, name="SPEAKER_00")
    make_speaker(db_session, normal_user, media_file, name="SPEAKER_01")
    index.voiceprints[str(first.uuid)] = VOICE
    db_session.commit()

    clusters = SpeakerClusteringService(db_session).cluster_speakers_for_file(
        media_file.id, normal_user.id
    )

    assert len(clusters) == 1
    assert _clusters_of(db_session, normal_user) == 1


# ---------------------------------------------------------------------------
# update_cluster_centroid: a member whose voiceprint cannot be read
# ---------------------------------------------------------------------------


def _two_member_cluster(db, user, index: _SpeakerIndex) -> tuple[Any, Any]:
    a = _speaker(db, user, None, index, VOICE)
    b = _speaker(db, user, None, index, OTHER)
    index.unreadable.add(str(b.uuid))
    return make_cluster(db, user, a, b), b


def test_a_centroid_is_not_rebuilt_from_the_members_that_answered(
    db_session, normal_user, index, speaker_os_seams
):
    cluster, _ = _two_member_cluster(db_session, normal_user, index)

    SpeakerClusteringService(db_session)._update_cluster_centroid(cluster, normal_user.id)

    assert speaker_os_seams["clusters"] == {}


def test_a_strict_centroid_rebuild_reports_the_unreadable_member(
    db_session, normal_user, index, speaker_os_seams
):
    cluster, _ = _two_member_cluster(db_session, normal_user, index)

    with pytest.raises(CentroidRecomputeError):
        SpeakerClusteringService(db_session)._update_cluster_centroid(
            cluster, normal_user.id, strict=True
        )
    assert speaker_os_seams["clusters"] == {}


def test_a_centroid_with_every_member_readable_is_rebuilt(
    db_session, normal_user, index, speaker_os_seams
):
    cluster, b = _two_member_cluster(db_session, normal_user, index)
    index.unreadable.discard(str(b.uuid))

    SpeakerClusteringService(db_session)._update_cluster_centroid(cluster, normal_user.id)

    assert list(speaker_os_seams["clusters"]) == [str(cluster.uuid)]
