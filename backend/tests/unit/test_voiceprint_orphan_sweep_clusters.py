"""The voiceprint orphan sweep removes cluster centroids whose ``speaker_cluster`` row is gone.

A purge interrupted before ``privacy_purge_pending`` existed left ``cluster_<uuid>``
documents behind with nothing to retry them. The sweep is their backstop, so it must delete
exactly those and fail closed when it cannot read the table that says which centroids live.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.services import voiceprint_orphan_sweep as sweep

_INDEX = "speakers_v4"
_LIVE = "11111111-1111-1111-1111-111111111111"
_ORPHAN = "22222222-2222-2222-2222-222222222222"
_SPEAKER = "33333333-3333-3333-3333-333333333333"


def _hits() -> list[dict[str, Any]]:
    return [
        {"_id": _SPEAKER, "_source": {}},
        {
            "_id": f"cluster_{_LIVE}",
            "_source": {"document_type": "cluster", "cluster_uuid": _LIVE},
        },
        {
            "_id": f"cluster_{_ORPHAN}",
            "_source": {"document_type": "cluster", "cluster_uuid": _ORPHAN},
        },
    ]


def _run(
    *, cluster_uuids: Any, deleted: list[str] | None = None
) -> tuple[dict[str, Any], list[str]]:
    deleted = [] if deleted is None else deleted
    client = MagicMock()

    def delete_by_query(index, body, **_kw):  # opensearch-py shape
        ids = body["query"]["ids"]["values"]
        deleted.extend(ids)
        return {"deleted": len(ids)}

    client.delete_by_query.side_effect = delete_by_query
    client.count.return_value = {"count": 0}
    with (
        patch("app.services.opensearch_service.get_opensearch_client", return_value=client),
        patch(
            "app.services.opensearch_service.speaker_maintenance.speaker_embedding_indices",
            return_value=[_INDEX],
        ),
        patch.object(sweep, "_concrete_indices", return_value=[_INDEX]),
        patch.object(sweep, "_db_speaker_uuids", return_value={_SPEAKER}),
        patch.object(sweep, "_db_profile_uuids", return_value=set()),
        patch.object(sweep, "_db_cluster_uuids", create=True, **cluster_uuids),
        patch("opensearchpy.helpers.scan", return_value=iter(_hits())),
    ):
        report = sweep.sweep_orphan_voiceprints(dry_run=False)
    return report, deleted


@pytest.mark.unit
def test_only_the_centroid_without_a_row_is_deleted() -> None:
    report, deleted = _run(cluster_uuids={"return_value": {_LIVE}})
    assert deleted == [f"cluster_{_ORPHAN}"]
    assert report["indices"][_INDEX]["orphan_cluster_docs"] == 1


@pytest.mark.unit
def test_an_unreadable_cluster_table_deletes_nothing() -> None:
    deleted: list[str] = []
    with pytest.raises(RuntimeError, match="db down"):
        _run(cluster_uuids={"side_effect": RuntimeError("db down")}, deleted=deleted)
    assert deleted == []
