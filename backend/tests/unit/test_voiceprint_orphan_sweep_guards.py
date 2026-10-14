"""The voiceprint orphan sweep must carry the same guards as the index orphan sweep.

``speaker_embedding_consistency_check`` runs from beat every 10 minutes and deletes
every speaker embedding whose speaker is not in Postgres. It had NONE of the guards
``opensearch_integrity_task`` grew after the June 2026 incident (Postgres restored
EMPTY, OpenSearch intact): an empty or truncated Postgres made every voiceprint an
"orphan", and the sweep removed them all — biometric data that, unlike an index
document, cannot be rebuilt from Postgres (it is re-extracted from audio, per file, on
a GPU). It also compared against a snapshot taken BEFORE reading the index, so a
speaker created and indexed in that window was deleted as an orphan.

These tests drive the real task body with the stores replaced by recorders, so what
is asserted is exactly which voiceprints it would delete.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.tasks import speaker_embedding_consistency as task_mod


class _Redis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set(self, key, value, nx=False, ex=None):  # noqa: ANN001 — redis-py shape
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    def delete(self, *keys):  # noqa: ANN001
        for key in keys:
            self.store.pop(key, None)

    def scan_iter(self, match=None):  # noqa: ANN001
        return iter(())


def _run(
    *,
    os_uuids: set[str],
    pg_with_segments: set[str],
    all_pg: set[str],
    present_at_recheck: set[str] | None = None,
) -> list[str]:
    """Run the check once; return the speaker uuids it removed from OpenSearch."""
    removed: list[str] = []
    client = MagicMock()
    client.indices.exists.return_value = False
    with (
        patch.object(task_mod, "get_redis", return_value=_Redis()),
        patch("app.services.migration_progress_service.migration_progress") as progress,
        patch.object(
            task_mod,
            "_get_pg_speaker_uuids_with_segments",
            return_value=dict.fromkeys(pg_with_segments, 1),
        ),
        patch.object(task_mod, "_get_all_pg_speaker_uuids", return_value=set(all_pg)),
        patch.object(task_mod, "_get_opensearch_speaker_uuids", return_value=set(os_uuids)),
        patch(
            "app.services.embedding_mode_service.EmbeddingModeService.get_current_mode",
            return_value="v3",
        ),
        patch("app.services.opensearch_service.get_opensearch_client", return_value=client),
        patch("app.services.opensearch_service.remove_speaker_embedding", removed.append),
        patch.object(
            task_mod,
            "_speaker_uuids_present",
            side_effect=lambda keys: {k for k in keys if k in (present_at_recheck or set())},
            # create=True so the same test runs (and fails on behaviour, not on a missing
            # name) against the pre-guard module that had no re-check at all.
            create=True,
        ),
        patch.object(task_mod, "send_ws_event"),
    ):
        progress.is_running.return_value = False
        task_mod.speaker_embedding_consistency_check_task.run()
    return sorted(removed)


def _uuids(prefix: str, n: int) -> set[str]:
    return {f"{prefix}-{i:04d}" for i in range(n)}


@pytest.mark.unit
def test_an_empty_postgres_deletes_no_voiceprints() -> None:
    """The June 2026 shape: Postgres restored empty, OpenSearch intact."""
    assert _run(os_uuids=_uuids("vp", 40), pg_with_segments=set(), all_pg=set()) == []


@pytest.mark.unit
def test_a_sweep_removing_most_of_the_index_is_refused() -> None:
    """A truncated Postgres (most speakers missing) is a lost database, not garbage."""
    kept = _uuids("kept", 5)
    gone = _uuids("gone", 60)
    assert _run(os_uuids=kept | gone, pg_with_segments=kept, all_pg=kept) == []


@pytest.mark.unit
def test_a_speaker_created_after_the_snapshot_is_not_deleted() -> None:
    """In OpenSearch, absent from the stale snapshot, present when re-asked."""
    kept = _uuids("kept", 50)
    new_speaker = "new-0000"
    real_orphan = "orphan-0000"
    removed = _run(
        os_uuids=kept | {new_speaker, real_orphan},
        pg_with_segments=kept,
        all_pg=kept,
        present_at_recheck={new_speaker},
    )
    assert removed == [real_orphan]


@pytest.mark.unit
def test_routine_orphans_are_still_removed() -> None:
    """The control: a handful of genuine orphans in a healthy index still go."""
    kept = _uuids("kept", 50)
    orphans = _uuids("orphan", 3)
    removed = _run(os_uuids=kept | orphans, pg_with_segments=kept, all_pg=kept)
    assert removed == sorted(orphans)
