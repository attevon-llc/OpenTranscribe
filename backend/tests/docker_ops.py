"""One budget for starting a throwaway container, in one place.

``backend/tests/CLAUDE.md`` states the rule this module exists to make enforceable:

    Rules that follow: give the constant a **name**, put the **measurement** beside it,
    and never copy a budget into a fifth file.

It was being broken. ``docker run alpine`` at 60 s had already been raised to
``_DOCKER_OP_TIMEOUT = 120`` **plus one retry** in
``tests/unit/test_opentr_stop_container_scoping.py`` — but that constant was private to
that module, so the two throwaway-Redis fixtures each carried their own hardcoded
``timeout=30``, less than half the value the same host had already been measured to need.

Measured 2026-09-08, on a full ``run-dev-tests.sh --full``: ``docker run -d --rm -p
127.0.0.1:<port>:6379 redis:7-alpine`` **timed out at 30 s**, failing **19 tests** — all 18
of ``test_migration_progress_service`` (one ``xdist_group``, so every one of them died in
fixture setup) plus ``test_backup_tasks``'s real-Redis lock test. The identical command
takes a couple of seconds on an idle host. Nothing was wrong with the code under test; the
gate was the load, exactly as that document warns.

⚠️ **The retry is not belt-and-braces, it is the point.** The failure mode is a daemon that
is momentarily saturated (the gate runs 48 pytest workers, 3 Playwright workers and image
builds against one dockerd). A single long timeout still fails if the daemon is busy at the
one moment you ask; asking again a moment later is what actually recovers. And a timeout
here is a **fixture** failure, which reports as an ERROR against every test in the group —
so one contended second costs an entire module.
"""

from __future__ import annotations

import subprocess
import time

#: Budget for a single docker CLI operation that starts or stops a container.
#:
#: 120 s, not because a container takes that long — it takes seconds — but because the
#: gate itself contends the daemon. Same value, and the same reasoning, as the one proven
#: by ``test_opentr_stop_container_scoping.py``; it lives here so there is one of it.
DOCKER_OP_TIMEOUT = 120

#: How many times to attempt the run in total (so: one retry).
DOCKER_RUN_ATTEMPTS = 2

#: Pause between attempts, to let a momentary daemon spike pass.
DOCKER_RETRY_PAUSE_S = 3.0


class DockerOpError(RuntimeError):
    """A docker operation failed or timed out on every attempt.

    Deliberately a distinct type: callers must be able to tell "docker is not available
    here, skip" (checked separately, before calling) from "docker IS here and could not
    do this", which is a real environment problem and must fail loudly rather than skip.
    """


def run_container(
    args: list[str],
    *,
    timeout: int = DOCKER_OP_TIMEOUT,
    attempts: int = DOCKER_RUN_ATTEMPTS,
) -> subprocess.CompletedProcess[str]:
    """Run ``docker <args>``, retrying once on timeout or failure.

    Returns the completed process on success; raises :class:`DockerOpError` with every
    attempt's diagnosis when all attempts fail. The message names the elapsed time and the
    stderr of the last attempt, because "it timed out" without the budget is unactionable.
    """
    problems: list[str] = []
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        try:
            return subprocess.run(
                ["docker", *args],
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            problems.append(f"attempt {attempt}: timed out after {timeout}s")
        except subprocess.CalledProcessError as exc:
            elapsed = time.monotonic() - started
            problems.append(
                f"attempt {attempt}: exit {exc.returncode} after {elapsed:.1f}s: "
                f"{(exc.stderr or '').strip()[:300]}"
            )
        if attempt < attempts:
            time.sleep(DOCKER_RETRY_PAUSE_S)

    raise DockerOpError(
        "docker " + " ".join(args[:3]) + " ... failed on every attempt:\n  " + "\n  ".join(problems)
    )


def stop_container(name: str, *, timeout: int = DOCKER_OP_TIMEOUT) -> None:
    """Best-effort teardown.

    Never raises: this runs in fixture teardown, where masking the test's own failure with
    a cleanup error loses the finding. ``--rm`` removes the container once stopped.
    """
    subprocess.run(["docker", "stop", name], capture_output=True, timeout=timeout, check=False)
