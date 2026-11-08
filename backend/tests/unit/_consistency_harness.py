"""Drive the real embedding-consistency orchestrator and repair batches over fake stores.

Postgres and OpenSearch are replaced by in-memory answers; Redis is real (the caller passes
the throwaway ``local_model_redis``), because the lease, the progress record and the
completion check ARE Redis behaviour. The GPU phase is replaced by a recorder that walks
the batch's files the way ``process_batch_pipelined`` does (``is_running_check`` before
each file, then the per-file callback).
"""

from __future__ import annotations

import time
import uuid as uuid_pkg
from collections.abc import Callable
from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

from app.tasks import embedding_consistency_repair as repair_mod
from app.tasks import speaker_embedding_consistency as task_mod

#: Every Redis key a consistency run writes (the tests delete them before and after).
RUN_KEYS = (
    "embedding_consistency_running",
    "embedding_consistency_progress",
    "embedding_consistency_last_run",
    "embedding_consistency:batch_task_ids",
    "embedding_consistency:failed_files",
)


def library(files: int, speakers_per_file: int, big_file_speakers: int | None = None):
    """``({speaker_uuid: file_id}, {file_id: file_uuid})``; file 1 may be oversized."""
    pg: dict[str, int] = {}
    fuuids: dict[int, str] = {}
    for fid in range(1, files + 1):
        fuuids[fid] = str(uuid_pkg.uuid4())
        count = big_file_speakers if (fid == 1 and big_file_speakers) else speakers_per_file
        for _ in range(count):
            pg[str(uuid_pkg.uuid4())] = fid
    return pg, fuuids


def run_check(
    redis_client: Any,
    pg: dict[str, int],
    fuuids: dict[int, str],
    *,
    v4: bool = False,
    manual: bool = False,
    on_detect: Callable[[], None] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run the orchestrator once; return its result and the kwargs it published."""
    dispatched: list[dict[str, Any]] = []

    def _record(kwargs: dict, **_: object) -> MagicMock:
        dispatched.append(kwargs)
        return MagicMock(id=str(uuid_pkg.uuid4()))

    def _detect() -> dict[str, int]:
        if on_detect is not None:
            on_detect()
        return dict(pg)

    def _group(missing: set[str], uuid_to_file: dict[str, int]) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {}
        for s in missing:
            grouped.setdefault(fuuids[uuid_to_file[s]], []).append(s)
        return grouped

    client = MagicMock()
    client.indices.exists.return_value = v4
    with ExitStack() as stack:
        enter = stack.enter_context
        enter(patch.object(task_mod, "get_redis", return_value=redis_client))
        progress = enter(patch("app.services.migration_progress_service.migration_progress"))
        progress.is_running.return_value = False
        enter(patch.object(task_mod, "_get_pg_speaker_uuids_with_segments", _detect))
        enter(patch.object(task_mod, "_get_all_pg_speaker_uuids", return_value=set(pg)))
        enter(patch.object(task_mod, "_get_opensearch_speaker_uuids", return_value=set()))
        enter(patch.object(task_mod, "_split_by_voiceprint_policy", lambda m, _p: (set(m), set())))
        enter(patch.object(task_mod, "_filter_unrepairable_speakers", return_value=set()))
        enter(patch.object(task_mod, "_group_by_file", _group))
        enter(
            patch(
                "app.services.embedding_mode_service.EmbeddingModeService.get_current_mode",
                return_value="v4" if v4 else "v3",
            )
        )
        enter(patch("app.services.opensearch_service.get_opensearch_client", return_value=client))
        enter(patch("app.services.progress_tracker.ProgressTracker"))
        enter(
            patch.object(
                repair_mod.speaker_embedding_consistency_repair_batch_task,
                "apply_async",
                side_effect=_record,
            )
        )
        enter(patch.object(task_mod, "send_ws_event"))
        result = task_mod.speaker_embedding_consistency_check_task.run(manual=manual)
    return result, dispatched


def run_batch(
    redis_client: Any,
    kwargs: dict[str, Any],
    *,
    per_file_seconds: float = 0.0,
    failing: frozenset[str] = frozenset(),
    processed: list[str] | None = None,
) -> dict[str, Any]:
    """Run one repair batch with the GPU phase replaced; files in *failing* write nothing."""

    def _phase(_mode, _targets, _writer, files, file_written, is_running, _idx, **cb):
        done = 0
        for fuuid, _prep in files:
            if not is_running():
                break
            time.sleep(per_file_seconds)
            if processed is not None:
                processed.append(fuuid)
            if fuuid not in failing:
                file_written[fuuid] = file_written.get(fuuid, 0) + 1
            cb["on_file_done"](fuuid)
            done += 1
        return done

    def _prepare(fuuid: str) -> SimpleNamespace:
        return SimpleNamespace(file_uuid=fuuid, media_file_id=1, speakers=[])

    with ExitStack() as stack:
        enter = stack.enter_context
        enter(patch.object(repair_mod, "get_redis", return_value=redis_client))
        enter(patch.object(repair_mod, "_voiceprints_allowed", lambda _fid: True))
        enter(patch.object(repair_mod, "_run_repair_phase", _phase))
        enter(patch.object(repair_mod, "send_ws_event"))
        enter(patch("app.tasks.migration_pipeline.prepare_file", _prepare))
        enter(patch("app.tasks.migration_pipeline.cleanup_gpu_memory"))
        enter(patch("app.services.progress_tracker.ProgressTracker"))
        enter(patch("app.services.progress_tracker.emit_progress_notification"))
        result: dict[str, Any] = repair_mod.speaker_embedding_consistency_repair_batch_task.run(
            **kwargs
        )
        return result
