"""``sweep_orphan_voiceprints --apply`` never deletes a voiceprint written while it scanned.

The row snapshot is taken before the scan. A transcription finishing meanwhile commits its speaker
row after the snapshot and indexes its embedding before the scan reaches it, so the document reads
as an orphan. Deleting it leaves a live speaker with no voiceprint until the file is reprocessed.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.services import voiceprint_orphan_sweep as sweep

_INDEX = "speakers_v4"
_OLD_ORPHAN = "44444444-4444-4444-4444-444444444444"
_COMMITTED_DURING_SCAN = "55555555-5555-5555-5555-555555555555"
_NOT_YET_COMMITTED = "66666666-6666-6666-6666-666666666666"
_LIVE_PROFILE = "77777777-7777-7777-7777-777777777777"


def _run(hits: list[dict[str, Any]], speaker_snapshots: list[set[str]]) -> tuple[dict, list]:
    deleted: list[str] = []
    client = MagicMock()

    def delete_by_query(index, body, **_kw):
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
        patch.object(sweep, "_db_speaker_uuids", side_effect=speaker_snapshots),
        # Any row at all: an empty database is refused before a delete (its own guard).
        patch.object(sweep, "_db_profile_uuids", return_value={_LIVE_PROFILE}),
        patch.object(sweep, "_db_cluster_uuids", return_value=set()),
        patch("opensearchpy.helpers.scan", return_value=iter(hits)),
    ):
        report = sweep.sweep_orphan_voiceprints(dry_run=False)
    return report, deleted


@pytest.mark.unit
def test_a_speaker_committed_during_the_scan_keeps_its_voiceprint() -> None:
    long_ago = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    hits = [
        {"_id": _OLD_ORPHAN, "_source": {"created_at": long_ago}},
        {"_id": _COMMITTED_DURING_SCAN, "_source": {"created_at": long_ago}},
    ]
    # Before the scan the new speaker's row did not exist yet; by the delete it does.
    report, deleted = _run(hits, [set(), {_COMMITTED_DURING_SCAN}])

    assert deleted == [_OLD_ORPHAN]
    assert report["skipped_recent"] == 1


@pytest.mark.unit
def test_a_voiceprint_written_after_the_sweep_started_is_never_deleted() -> None:
    """Its row may still be uncommitted when the rows are read again."""
    just_now = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    hits = [{"_id": _NOT_YET_COMMITTED, "_source": {"created_at": just_now}}]

    report, deleted = _run(hits, [set(), set()])

    assert deleted == []
    assert report["orphans_found"] == 1 and report["skipped_recent"] == 1
