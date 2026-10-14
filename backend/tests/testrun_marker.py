"""A pytest process registers itself as a LIVE test run for ``scripts/cleanup-test-data.py``.

The sweep decides what it may delete by time: anything a test created at or after the start
of the oldest live run might still be in use, and is left alone. Until this module, only the
wrapper scripts (``run-integration-tests.sh``, ``e2e/run-e2e.sh``) registered a run, through
``scripts/testrun-registry.sh``. A bare ``pytest`` (how most agents and developers run a
file) registered nothing, so a concurrent sweep could not see it and would remove the
throwaway OpenSearch index, scratch database or ``/tmp`` tree it was in the middle of using.

The marker format and locking are the registry's: one ``started_at=<epoch>`` line in a file
under the registry directory, held with an exclusive ``flock`` for the life of the process.
The kernel drops the lock when the process dies, SIGKILL included, so a killed run reads as
stale on the next sweep and its leftovers become sweepable.

The registry directory is shared by every git worktree of the checkout (it resolves through
``git rev-parse --git-common-dir``), because two worktrees can target one stack; a marker in a
per-worktree directory would be invisible to a sweep started from another one.
``scripts/testrun-registry.sh`` and ``scripts/cleanup-test-data.py`` resolve the same path,
and ``unit/test_testrun_registry_location.py`` fails if the three ever disagree.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import shutil
import subprocess
import time
from pathlib import Path

#: Same override name the bash registry and the sweep honour.
REGISTRY_ENV = "TESTRUN_REGISTRY_DIR"


def default_testruns_dir(repo_root: Path) -> Path:
    """The ``.testruns`` directory of the MAIN checkout that owns ``repo_root``.

    Falls back to ``repo_root/.testruns`` outside a git checkout (an extracted archive).
    """
    override = os.environ.get(REGISTRY_ENV)
    if override:
        return Path(override)
    git = shutil.which("git")
    if git is None:
        return repo_root / ".testruns"
    argv = [git, "-C", str(repo_root), "rev-parse", "--path-format=absolute", "--git-common-dir"]
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv from a resolved binary, no shell
            argv,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return repo_root / ".testruns"
    common = result.stdout.strip()
    if result.returncode != 0 or not common:
        return repo_root / ".testruns"
    return Path(common).parent / ".testruns"


class LiveRunMarker:
    """Holds one flock-locked marker file until :meth:`release` (or process death)."""

    def __init__(self, testruns_dir: Path) -> None:
        self.path: Path | None = None
        self._fd: int | None = None
        try:
            testruns_dir.mkdir(parents=True, exist_ok=True)
            path = testruns_dir / f"{time.time_ns()}-{os.getpid()}-pytest.lock"
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        except OSError:
            # Never fail a test run over its liveness marker: the cost of a missing marker
            # is a sweep that is less conservative, which the sweep's own grace window bounds.
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(fd, f"started_at={int(time.time())}\n".encode())
        except OSError:
            os.close(fd)
            with contextlib.suppress(OSError):
                path.unlink()
            return
        self.path = path
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        if self.path is not None:
            with contextlib.suppress(OSError):
                self.path.unlink()
        with contextlib.suppress(OSError):
            os.close(self._fd)
        self._fd = None
        self.path = None
