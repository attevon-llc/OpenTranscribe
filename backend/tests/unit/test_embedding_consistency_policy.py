"""Documents missing from an OFF scope are the policy working, not damage (#1280, plan D5).

The consistency check compares Postgres speakers with the voiceprint index and queues GPU
repair for what is missing. A scope whose voiceprint switch is off legitimately has no
documents (a purge, a skipped migration, a refused write), so reporting them as missing made
the status panel claim damage and queued repair work the batch could only skip. These tests
drive the real orchestrator and the dry-run counts with the stores replaced by recorders and
the policy read from real rows, so what is asserted is what would be reported and queued.
"""

from __future__ import annotations

import uuid as uuid_pkg
from contextlib import contextmanager
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.tasks import embedding_consistency_repair as repair_mod
from app.tasks import speaker_embedding_consistency as task_mod
from tests.fixtures.privacy_switches import file_in
from tests.fixtures.privacy_switches import make_org


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


@pytest.fixture
def world(db_session, normal_user):
    """Two speakers, both with segments and no indexed voiceprint: one in an allowed scope
    (personal) and one in an organization that has voiceprints off."""
    off_org = make_org(db_session, voiceprints=False)
    allowed_file = file_in(db_session, normal_user, None)
    off_file = file_in(db_session, normal_user, off_org)
    db_session.commit()
    return {
        "allowed": (str(uuid_pkg.uuid4()), allowed_file.id),
        "off": (str(uuid_pkg.uuid4()), off_file.id),
        "off_org": off_org,
    }


@contextmanager
def _stores(db_session, pg_speakers: dict[str, int]):
    """The orchestrator and the counts with Postgres and OpenSearch replaced by recorders."""
    client = MagicMock()
    client.indices.exists.return_value = False

    @contextmanager
    def _session():
        yield db_session

    dispatched: list[dict] = []

    def _record(kwargs: dict, **_: object) -> MagicMock:
        dispatched.append(kwargs)
        return MagicMock(id="t")

    with (
        patch.object(task_mod, "get_redis", return_value=_Redis()),
        patch.object(task_mod, "session_scope", _session),
        patch("app.services.migration_progress_service.migration_progress") as progress,
        patch.object(task_mod, "_get_pg_speaker_uuids_with_segments", return_value=pg_speakers),
        patch.object(task_mod, "_get_all_pg_speaker_uuids", return_value=set(pg_speakers)),
        patch.object(task_mod, "_get_opensearch_speaker_uuids", return_value=set()),
        patch(
            "app.services.embedding_mode_service.EmbeddingModeService.get_current_mode",
            return_value="v3",
        ),
        patch("app.services.opensearch_service.get_opensearch_client", return_value=client),
        patch("app.services.progress_tracker.ProgressTracker"),
        patch.object(
            repair_mod.speaker_embedding_consistency_repair_batch_task,
            "apply_async",
            side_effect=_record,
        ),
        patch.object(task_mod, "send_ws_event"),
    ):
        progress.is_running.return_value = False
        yield dispatched


def _speakers(world, *names: str) -> dict[str, int]:
    return dict(world[n] for n in names)


def test_a_missing_document_in_an_off_scope_is_healthy_and_queues_nothing(db_session, world):
    with _stores(db_session, _speakers(world, "off")) as dispatched:
        result = task_mod.speaker_embedding_consistency_check_task.run()

    assert result["status"] == "healthy"
    assert result["skipped_by_policy"] == 1
    assert dispatched == []


def test_the_same_missing_document_in_an_allowed_scope_is_still_repaired(db_session, world):
    """The control: the shape is identical, only the scope differs."""
    with _stores(db_session, _speakers(world, "allowed")) as dispatched:
        result = task_mod.speaker_embedding_consistency_check_task.run()

    assert result["status"] == "repairing"
    assert result["v3_missing"] == 1
    assert len(dispatched) == 1
    assert dispatched[0]["missing_v3_uuids"] == [world["allowed"][0]]


def test_a_mixed_check_repairs_only_the_allowed_scope(db_session, world):
    with _stores(db_session, _speakers(world, "allowed", "off")) as dispatched:
        result = task_mod.speaker_embedding_consistency_check_task.run()

    assert result["status"] == "repairing"
    assert result["v3_missing"] == 1
    assert result["skipped_by_policy"] == 1
    assert [kw["missing_v3_uuids"] for kw in dispatched] == [[world["allowed"][0]]]


def test_the_dry_run_counts_leave_an_off_scope_out_of_missing(db_session, world):
    with _stores(db_session, _speakers(world, "allowed", "off")):
        counts = task_mod.get_embedding_consistency_counts()

    assert counts["main_missing"] == 1
    assert counts["skipped_by_policy"] == 1
