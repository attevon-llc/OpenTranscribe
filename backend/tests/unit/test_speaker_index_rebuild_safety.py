"""``repair.rebuild_speaker_index`` must never destroy the last copy of a voiceprint.

The rebuild is the last-resort repair for a corrupted ``speakers`` index: load a
temporary index from ``speakers_v4``, delete the corrupted index, recreate it,
copy back. Every failure between those steps used to lose every embedding —
they cannot be recomputed without the original media.

The OpenSearch client is stubbed, so no real index is touched. The Speaker row
these tests need is created in the savepoint-rolled-back ``db_session``.
"""

import uuid as uuid_pkg
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerCluster
from app.services.opensearch_service import repair

_SPEAKER_INDEX = "speakers"
_V4_INDEX = "speakers_v4"
_REBUILD_INDEX = "speakers_rebuild"

_CLIENT_PATH = "app.services.opensearch_service.client.opensearch_client"
_DIMENSION_PATH = "app.services.opensearch_service.repair.get_speaker_embedding_dimension"


@pytest.fixture
def clustered_speaker(db_session, normal_user):
    """One Speaker with a cluster assignment — what the rebuild reads from Postgres.

    Returns:
        The speaker's UUID as a string, which is the key the v4 scan is matched on.
    """
    cluster = SpeakerCluster(
        uuid=uuid_pkg.uuid4(),
        user_id=normal_user.id,
        label=f"rebuild-test-{uuid_pkg.uuid4().hex[:8]}",
        member_count=1,
    )
    db_session.add(cluster)
    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=normal_user.id,
        filename=f"rebuild-test-{uuid_pkg.uuid4().hex[:8]}.mp4",
        content_type="video/mp4",
        file_size=1000,
        storage_path=f"rebuild-test/{uuid_pkg.uuid4().hex}",
    )
    db_session.add(media)
    db_session.flush()

    speaker = Speaker(
        uuid=uuid_pkg.uuid4(),
        name=f"rebuild-test-{uuid_pkg.uuid4().hex[:8]}",
        user_id=normal_user.id,
        media_file_id=media.id,
        cluster_id=cluster.id,
    )
    db_session.add(speaker)
    db_session.flush()
    return str(speaker.uuid)


def _exists_map(**presence: bool):
    """Build an ``indices.exists`` side effect from index name to presence."""

    def _exists(index: str, **_kwargs) -> bool:
        return presence.get(index, False)

    return _exists


def _v4_page(speaker_uuid: str) -> dict[str, Any]:
    """One page of the ``speakers_v4`` scan holding a single recoverable speaker."""
    return {
        "hits": {
            "hits": [
                {
                    "_id": speaker_uuid,
                    "sort": ["a"],
                    "_source": {
                        "speaker_uuid": speaker_uuid,
                        "speaker_id": 1,
                        "user_id": 1,
                        "name": "spk",
                        "embedding": [0.1, 0.2],
                    },
                }
            ]
        }
    }


_EMPTY_PAGE: dict[str, Any] = {"hits": {"hits": []}}


def _deleted_indices(client: MagicMock) -> list[str]:
    """Index names passed to ``indices.delete``, in call order."""
    return [call.kwargs.get("index") for call in client.indices.delete.call_args_list]


def test_rebuild_refuses_when_nothing_can_be_recovered(db_session):
    """Defect: an absent ``speakers_v4`` meant "delete everything, create empty".

    With no embeddings to restore, the old code still deleted the corrupted
    ``speakers`` index and created a fresh empty one — converting an index that a
    snapshot could still have rescued into permanent loss of every voiceprint.
    """
    client = MagicMock()
    client.indices.exists.side_effect = _exists_map(**{_SPEAKER_INDEX: True})
    client.count.return_value = {"count": 0}

    with patch(_CLIENT_PATH, client), patch(_DIMENSION_PATH, return_value=256):
        result = repair.rebuild_speaker_index(db_session)

    assert result["status"] == "refused"
    assert _deleted_indices(client) == []


def test_short_bulk_load_leaves_the_corrupted_index_in_place(db_session, clustered_speaker):
    """Defect: the rebuild index was never verified before the delete.

    One document was fetched and loaded, the rebuild index holds none of it, and
    the old code went straight on to delete ``speakers`` anyway — losing the
    embeddings it had just failed to copy.
    """
    client = MagicMock()
    client.indices.exists.side_effect = _exists_map(**{_SPEAKER_INDEX: True, _V4_INDEX: True})
    client.search.side_effect = [_v4_page(clustered_speaker), _EMPTY_PAGE]
    client.bulk.return_value = {"errors": False}
    client.count.return_value = {"count": 0}

    with patch(_CLIENT_PATH, client), patch(_DIMENSION_PATH, return_value=256):
        result = repair.rebuild_speaker_index(db_session)

    assert result["status"] == "error"
    assert _deleted_indices(client) == []


def test_copy_back_shortfall_preserves_the_rebuild_index(db_session, clustered_speaker):
    """Defect: the temporary index was deleted whether or not the copy worked.

    By step 9 the corrupted original is already gone, so the rebuild index holds
    the only complete copy. A failed copy-back followed by an unconditional
    cleanup deleted it — the exact "failure between them loses all voiceprints"
    window. It must survive, and be named in the result.
    """
    client = MagicMock()
    client.indices.exists.side_effect = _exists_map(
        **{_SPEAKER_INDEX: True, _V4_INDEX: True, _REBUILD_INDEX: True}
    )
    client.search.side_effect = [
        _v4_page(clustered_speaker),
        _EMPTY_PAGE,
        _v4_page(clustered_speaker),
        _EMPTY_PAGE,
    ]
    client.bulk.side_effect = [{"errors": False}, {"errors": True, "items": []}]
    client.count.return_value = {"count": 1}

    with patch(_CLIENT_PATH, client), patch(_DIMENSION_PATH, return_value=256):
        result = repair.rebuild_speaker_index(db_session)

    assert result["recovery_index"] == _REBUILD_INDEX
    assert _deleted_indices(client) == [_REBUILD_INDEX, _SPEAKER_INDEX]


def test_complete_rebuild_reports_the_copied_count(db_session, clustered_speaker):
    """Control: a rebuild that copies everything back succeeds and cleans up.

    Without this the three refusals above could be satisfied by a function that
    never rebuilds at all.
    """
    client = MagicMock()
    client.indices.exists.side_effect = _exists_map(**{_SPEAKER_INDEX: True, _V4_INDEX: True})
    client.search.side_effect = [
        _v4_page(clustered_speaker),
        _EMPTY_PAGE,
        _v4_page(clustered_speaker),
        _EMPTY_PAGE,
    ]
    client.bulk.return_value = {"errors": False}
    client.count.return_value = {"count": 1}

    with patch(_CLIENT_PATH, client), patch(_DIMENSION_PATH, return_value=256):
        result = repair.rebuild_speaker_index(db_session)

    assert result["status"] == "rebuilt"
    assert result["speakers_indexed"] == 1
