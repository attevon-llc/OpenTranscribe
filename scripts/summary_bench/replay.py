"""The merge-only replay engine behind `replay-merge`: resumable, checkpointed, optionally parallel."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from summary_bench.config import ExitCode, HarnessError

MERGE_ARMS = ('old', 'new')


def read_rows(path: Path) -> list[dict[str, Any]]:
    """Read a checkpoint file, ignoring a torn final line left by an interrupted write."""
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding='utf-8').splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def replay_merges(
    jobs: list[tuple[str, str]],
    do_merge: Callable[[str, str], dict[str, Any]],
    arm_scope: Callable[[str], Any],
    path: Path,
    *,
    concurrency: int = 1,
) -> list[dict[str, Any]]:
    """Run ``(source, merge_arm)`` jobs, appending each finished row to ``path`` at once.

    Rows already in ``path`` are kept and their jobs skipped, so an interrupted replay resumes.
    ``arm_scope(merge_arm)`` is a context manager patching the module-level rule globals
    (``inject_arm``). Those globals are process-wide, so two merge arms must never be in flight
    together: jobs are grouped by merge arm, each group runs inside one scope, and only jobs of the
    same arm overlap. Returns every row (resumed and new), in job order.
    """
    if concurrency < 1:
        raise HarnessError('--concurrency must be at least 1', ExitCode.MISUSE)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = {(r['source'], r['merge_arm']): r for r in read_rows(path)}
    lock = threading.Lock()

    def work(job: tuple[str, str]) -> None:
        row = do_merge(*job)
        with lock:
            with path.open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(row, sort_keys=True) + '\n')
            rows[job] = row

    for arm in MERGE_ARMS:
        todo = [j for j in jobs if j[1] == arm and j not in rows]
        if not todo:
            continue
        with arm_scope(arm), ThreadPoolExecutor(max_workers=min(concurrency, len(todo))) as pool:
            for future in [pool.submit(work, j) for j in todo]:
                future.result()
    return [rows[j] for j in jobs if j in rows]
