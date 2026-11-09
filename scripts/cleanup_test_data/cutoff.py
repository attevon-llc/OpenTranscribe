"""Liveness cutoff (Decision 4): which test runs are still live, and the age floor."""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from cleanup_test_data.settings import _REPO_ROOT


def default_testruns_dir() -> Path:
    """The liveness registry: the MAIN checkout's ``.testruns`` (shared by every worktree).

    Same resolution as ``scripts/testrun-registry.sh`` and ``backend/tests/testrun_marker.py``
    (``unit/test_testrun_registry_location.py`` pins the three together). A per-worktree
    directory hid a run in one worktree from a sweep started in another against the same
    stack. ``TESTRUN_REGISTRY_DIR`` overrides it, for tests that need a throwaway registry.
    """
    override = os.environ.get('TESTRUN_REGISTRY_DIR')
    if override:
        return Path(override)
    git = shutil.which('git')
    if git is None:
        return _REPO_ROOT / '.testruns'
    argv = [git, '-C', str(_REPO_ROOT), 'rev-parse', '--path-format=absolute', '--git-common-dir']
    try:
        result = subprocess.run(  # fixed argv from a resolved binary, no shell
            argv, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return _REPO_ROOT / '.testruns'
    common = result.stdout.strip()
    if result.returncode != 0 or not common:
        return _REPO_ROOT / '.testruns'
    return Path(common).parent / '.testruns'


_TESTRUNS_DIR = default_testruns_dir()


# ---------------------------------------------------------------------------------------------
# Liveness cutoff (Decision 4) — pure functions, no I/O side effects beyond marker reads.
# ---------------------------------------------------------------------------------------------

_STARTED_AT_RE = re.compile(r'started_at=(\d+)')


def _read_started_at(marker: Path) -> int | None:
    try:
        content = marker.read_text(encoding='utf-8')
    except OSError:
        return None
    match = _STARTED_AT_RE.search(content)
    return int(match.group(1)) if match else None


def live_marker_start_times(testruns_dir: Path) -> list[int]:
    """``started_at`` epoch seconds for every LIVE run marker under *testruns_dir*.

    A marker is live iff its ``flock`` is currently held by another process, probed
    non-blockingly so this call never waits. If probing SUCCEEDS in acquiring the
    lock, nobody was holding it — the run that created it is gone (crashed, or exited
    without ever removing the file) — so it is stale and excluded, and the lock is
    released again immediately (this process does not want to hold it).
    """
    if not testruns_dir.is_dir():
        return []
    live: list[int] = []
    for marker in sorted(testruns_dir.glob('*.lock')):
        try:
            fd = os.open(str(marker), os.O_RDONLY)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # Could not acquire -> someone else holds it -> live.
            started_at = _read_started_at(marker)
            if started_at is not None:
                live.append(started_at)
        else:
            # Acquired it ourselves -> nobody was holding it -> stale.
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    return live


def apply_age_floor(cutoff: int, min_age_minutes: int, *, now: int | None = None) -> int:
    """Lower the cutoff so nothing younger than ``min_age_minutes`` is ever a candidate.

    The liveness markers protect every REGISTERED run; the floor protects the rest — an ad
    hoc script, a browser session, a test started from a checkout whose registry this sweep
    cannot see. ``scripts/cleanup-test-leftovers.sh`` runs unattended, so it always sets one.
    """
    if min_age_minutes <= 0:
        return cutoff
    resolved_now = now if now is not None else int(time.time())
    return min(cutoff, resolved_now - min_age_minutes * 60)


def resolve_cutoff(live_starts: list[int], *, now: int | None = None) -> int:
    """The sweep cutoff: the oldest currently-live run's start time, or now if none.

    Pure function — no I/O — so it is trivially unit-testable. A row created BEFORE
    this cutoff belongs to no currently-live run and is safe to consider; a row
    created at or after it might belong to a run still in progress and must be left
    alone. ``now`` always participates (a caller's own just-registered marker
    included), so a fresh, single, isolated run still gets a sane (very recent)
    cutoff rather than an unbounded one.
    """
    resolved_now = now if now is not None else int(time.time())
    return min([*live_starts, resolved_now])


def prune_stale_markers(testruns_dir: Path, *, execute: bool, grace_seconds: int) -> list[Path]:
    """Marker files no process holds any more (their run exited or was killed).

    A marker younger than ``grace_seconds`` is never pruned: ``testrun_begin`` creates the
    file a moment BEFORE it takes the lock, and deleting it inside that window would leave
    the run holding a lock on an unlinked file, invisible to every later sweep.
    """
    if not testruns_dir.is_dir():
        return []
    stale: list[Path] = []
    for marker in sorted(testruns_dir.glob('*.lock')):
        try:
            if time.time() - marker.stat().st_mtime < grace_seconds:
                continue
            fd = os.open(str(marker), os.O_RDONLY)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            continue  # held: a live run
        else:
            stale.append(marker)
            if execute:
                with contextlib.suppress(OSError):
                    marker.unlink()
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    return stale
