"""The service rename sites, end to end into a real chunk index (issue #432).

``tests/api/test_speaker_rename_service_sites.py`` proves each service path
*dispatches*. This proves the whole chain does the thing the dispatch exists for:
Postgres rename → tracker → ``dispatch_speaker_rename`` → ``update_by_query`` →
the renamed speaker's earlier words are reachable under the new name.

Only a real cluster can show that. ``speaker`` is a ``keyword`` field and chat's
speaker axis is an exact ``terms`` match on it — the bug is entirely a property of
that mapping, so a stand-in index would confirm nothing. Every test carries its
own negative control: the pre-#432 state, asserted live, before the rename runs.

The Celery hop is collapsed with ``.apply()`` rather than mocked away: the task
body, the painless script and the refresh all still execute, in-process.

Point it at an isolated stack — never the shared dev one::

    OPENSEARCH_PORT=5280 POSTGRES_PORT=5276 \\
        pytest backend/tests/integration/test_speaker_rename_service_chunks.py -m integration
"""

from __future__ import annotations

import os
import uuid as uuid_mod
from typing import Any
from unittest.mock import patch

import pytest

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerCluster
from app.models.media import SpeakerClusterMember

_OPENSEARCH_ABSENT = os.environ.get("SKIP_OPENSEARCH", "True").lower() == "true"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        _OPENSEARCH_ABSENT,
        reason=(
            "No OpenSearch reachable (SKIP_OPENSEARCH). Start an isolated stack and "
            "export OPENSEARCH_PORT — a stand-in cannot validate update_by_query."
        ),
    ),
]

USER_ID = 4320


def _doc(file_uuid: str, chunk_index: int, speaker: str, speakers: list[str], content: str):
    """One chunk document shaped as the indexing service writes them."""
    return {
        "file_id": 432,
        "file_uuid": file_uuid,
        "user_id": USER_ID,
        "chunk_index": chunk_index,
        "content": content,
        "title": "Q3 pricing sync",
        "speaker": speaker,
        "speakers": speakers,
        "tags": [],
        "content_type": "audio/wav",
        "accessible_user_ids": [USER_ID],
        "upload_time": "2026-08-01T00:00:00+00:00",
        "language": "en",
        "start_time": float(chunk_index * 10),
        "end_time": float(chunk_index * 10 + 9),
        "indexed_at": "2026-08-01T00:00:00+00:00",
    }


@pytest.fixture
def chunk_index(monkeypatch):
    """A throwaway chunks index with the REAL mapping, wired in via settings."""
    from app.core.config import settings
    from app.services.opensearch_service import get_opensearch_client
    from app.services.search import indexing_service as svc

    client = get_opensearch_client()
    assert client is not None, "SKIP_OPENSEARCH said a cluster was reachable but it is not"

    name = f"test_rename_sites_{uuid_mod.uuid4().hex[:12]}"
    client.indices.create(index=name, body=svc._get_index_body_with_dimension(384))
    monkeypatch.setattr(settings, "OPENSEARCH_CHUNKS_INDEX", name)
    try:
        yield client
    finally:
        client.indices.delete(index=name, ignore=[404])


@pytest.fixture
def run_propagation_inline():
    """Execute the queued task in-process — the task body itself is under test."""
    from app.tasks.rename_propagation_task import propagate_speaker_rename

    with patch(
        "app.tasks.rename_propagation_task.propagate_speaker_rename.delay",
        side_effect=lambda **kwargs: propagate_speaker_rename.apply(kwargs=kwargs),
    ) as delay_mock:
        yield delay_mock


def _index_docs(client, docs: list[dict[str, Any]]) -> None:
    from app.core.config import settings

    for doc in docs:
        client.index(
            index=settings.OPENSEARCH_CHUNKS_INDEX,
            id=f"{doc['file_uuid']}_{doc['chunk_index']}",
            body=doc,
        )
    client.indices.refresh(index=settings.OPENSEARCH_CHUNKS_INDEX)


def _speakers_of(client, file_uuid: str) -> list[str]:
    from app.core.config import settings

    response = client.search(
        index=settings.OPENSEARCH_CHUNKS_INDEX,
        body={"size": 50, "query": {"term": {"file_uuid": file_uuid}}, "sort": ["chunk_index"]},
    )
    return [hit["_source"]["speaker"] for hit in response["hits"]["hits"]]


def _media_file(db_session, user, name: str) -> MediaFile:
    media_file = MediaFile(
        uuid=str(uuid_mod.uuid4()),
        user_id=user.id,
        filename=f"{name}.mp4",
        storage_path=f"test/{name}.mp4",
        content_type="video/mp4",
        file_size=1000,
    )
    db_session.add(media_file)
    db_session.flush()
    return media_file


def _speaker(db_session, user, media_file, name: str, **kwargs) -> Speaker:
    speaker = Speaker(
        uuid=str(uuid_mod.uuid4()),
        media_file_id=media_file.id,
        user_id=user.id,
        name=name,
        **kwargs,
    )
    db_session.add(speaker)
    db_session.flush()
    return speaker


def test_batch_accept_makes_pre_rename_chunks_reachable_under_the_new_name(
    db_session, normal_user, chunk_index, run_propagation_inline
):
    """The headline defect, driven from the service the inbox's "accept" button calls.

    Chat resolves the display name from Postgres and filters the index with an
    exact ``terms`` match. Before propagation, everything this speaker said is
    unreachable under the only name the user can ask about.
    """
    from app.services.search.chunk_retrieval import retrieve_chunks
    from app.services.speaker_clustering_service import SpeakerClusteringService

    media_file = _media_file(db_session, normal_user, "batch-accept")
    speaker = _speaker(db_session, normal_user, media_file, "SPEAKER_01", suggested_name="Dana")
    file_uuid = str(media_file.uuid)
    _index_docs(
        chunk_index,
        [
            _doc(file_uuid, 0, "SPEAKER_01", ["SPEAKER_01", "Ravi"], "pricing should go up"),
            _doc(file_uuid, 1, "Ravi", ["SPEAKER_01", "Ravi"], "pricing is fine as it stands"),
        ],
    )

    def _scoped(name: str):
        return retrieve_chunks(
            "pricing",
            user_id=USER_ID,
            file_uuids=[file_uuid],
            speakers=[name],
            search_mode="keyword",
        )

    assert _scoped("SPEAKER_01"), "control: the content is retrievable under the stale name"
    assert _scoped("Dana") == [], "the bug: the renamed speaker's words are unreachable"

    result = SpeakerClusteringService(db_session).batch_verify_speakers(
        [str(speaker.uuid)], int(normal_user.id), action="accept"
    )

    assert result["updated_count"] == 1
    db_session.refresh(speaker)
    assert speaker.display_name == "Dana"

    hits = _scoped("Dana")
    assert len(hits) == 1
    assert "pricing should go up" in hits[0].content
    assert _scoped("SPEAKER_01") == [], "and the stale name no longer resolves"
    assert _speakers_of(chunk_index, file_uuid) == ["Dana", "Ravi"], "Ravi was left alone"


def test_cluster_promotion_rewrites_every_file_the_cluster_spans(
    db_session, normal_user, chunk_index, run_propagation_inline
):
    """One promotion, two files, one ``update_by_query`` each — and no collateral."""
    from app.services.speaker_clustering_service import SpeakerClusteringService

    first = _media_file(db_session, normal_user, "promo-a")
    second = _media_file(db_session, normal_user, "promo-b")
    untouched = _media_file(db_session, normal_user, "promo-unrelated")
    alpha = _speaker(db_session, normal_user, first, "SPEAKER_00")
    beta = _speaker(db_session, normal_user, second, "SPEAKER_03", display_name="Unknown 3")

    cluster = SpeakerCluster(uuid=str(uuid_mod.uuid4()), user_id=normal_user.id, member_count=2)
    db_session.add(cluster)
    db_session.flush()
    for speaker in (alpha, beta):
        db_session.add(
            SpeakerClusterMember(
                uuid=str(uuid_mod.uuid4()),
                cluster_id=cluster.id,
                speaker_id=speaker.id,
                confidence=0.9,
            )
        )
    db_session.flush()

    _index_docs(
        chunk_index,
        [
            _doc(str(first.uuid), 0, "SPEAKER_00", ["SPEAKER_00"], "first recording"),
            _doc(str(second.uuid), 0, "Unknown 3", ["Unknown 3"], "second recording"),
            # Same diarizer label, a file the cluster does not reach into.
            _doc(str(untouched.uuid), 0, "SPEAKER_00", ["SPEAKER_00"], "unrelated recording"),
        ],
    )

    with patch(
        "app.services.profile_embedding_service.ProfileEmbeddingService.update_profile_embedding"
    ):
        profile = SpeakerClusteringService(db_session).promote_cluster_to_profile(
            str(cluster.uuid), "Dana", int(normal_user.id)
        )

    assert profile is not None
    assert run_propagation_inline.call_count == 2, "one task per file, never one per speaker"
    assert _speakers_of(chunk_index, str(first.uuid)) == ["Dana"]
    assert _speakers_of(chunk_index, str(second.uuid)) == ["Dana"]
    assert _speakers_of(chunk_index, str(untouched.uuid)) == ["SPEAKER_00"], (
        "a same-named speaker in an unrelated file must not be swept up"
    )
