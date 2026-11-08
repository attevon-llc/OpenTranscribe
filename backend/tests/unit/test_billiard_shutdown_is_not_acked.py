"""A job unwound by the pool's own terminate signal must be reported LOST, never completed.

Celery with ``acks_late`` acknowledges a task once the pool reports a result for it. billiard
4.3.0 reported the cold-shutdown ``SystemExit`` as that job's result, so the message was ACKED
and silently dropped; 4.3.1 reports nothing and the parent accounts for the job as lost, which
lets Celery reject-with-requeue it back to the head of its queue (see
``tests/integration/test_orphaned_delivery_recovery_live.py::
test_a_cold_shutdown_hands_a_running_stage_back_to_the_head_of_its_queue``).

This drives a real ``billiard.Pool`` in a child interpreter (forking the pytest process is not
safe) and fails if the shutdown ``SystemExit`` ever comes back as anything but a lost worker.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404 - runs this interpreter on a fixed script
import sys
import textwrap

import pytest

_SCRIPT = textwrap.dedent(
    """
    import os, signal, time
    import billiard
    from billiard.exceptions import WorkerLostError

    def job():
        os.kill(os.getpid(), signal.SIGTERM)  # the pool's own terminate handler runs here
        time.sleep(60)

    if __name__ == "__main__":
        pool = billiard.Pool(1)
        try:
            outcome = pool.apply_async(job).get(timeout=__POOL_WAIT_S__)
            print("RESULT", repr(outcome))
        except WorkerLostError:
            print("LOST")
        except BaseException as exc:  # noqa: BLE001 - report whatever came back
            # billiard re-raises a pickled traceback wrapper around the real error.
            print("LOST" if "WorkerLostError" in str(exc) else f"OTHER {type(exc).__name__}: {exc}")
        finally:
            pool.terminate()
    """
)


# Seconds the child waits on the pool, and the extra the parent allows for interpreter start-up,
# the billiard import and ``pool.terminate()``. The test takes ~12 s alone; the budget is 20x
# that at idle and grows with host load (a 48-way xdist run on a loaded host starved the
# 240 s fixed budget). A regression still fails fast: the pool returns RESULT, not a hang.
_POOL_WAIT_S = 150
_PROCESS_HEADROOM_S = 90
# The suite-wide pytest-timeout (300 s, pyproject addopts) used to sit BELOW the scaled budget
# once the load factor passed 1.25, so on a loaded sweep pytest-timeout killed the test first
# and the probe's own verdict (with the child's stdout) was never reported. The test carries its
# own ceiling, and the scaled budget is clamped under it.
_TEST_TIMEOUT_S = 900
_CLAMP_MARGIN_S = 30


def _load_factor() -> float:
    """1.0 on an idle host, rising with the 1-minute load average per CPU."""
    return max(1.0, os.getloadavg()[0] / (os.cpu_count() or 1))


def _budget(factor: float) -> tuple[int, int]:
    """``(pool wait, process timeout)`` scaled by *factor*, kept under the test's own timeout."""
    ceiling = _TEST_TIMEOUT_S - _CLAMP_MARGIN_S
    process_timeout = min(ceiling, int((_POOL_WAIT_S + _PROCESS_HEADROOM_S) * factor))
    pool_wait = min(int(_POOL_WAIT_S * factor), process_timeout - _PROCESS_HEADROOM_S)
    return pool_wait, process_timeout


def test_the_budget_always_ends_before_pytest_timeout_kills_the_test():
    for factor in (1.0, 1.25, 2.0, 10.0):
        pool_wait, process_timeout = _budget(factor)
        assert 0 < pool_wait < process_timeout < _TEST_TIMEOUT_S


@pytest.mark.timeout(_TEST_TIMEOUT_S)
def test_the_shutdown_systemexit_is_reported_as_a_lost_worker_not_a_result():
    factor = _load_factor()
    pool_wait, process_timeout = _budget(factor)
    try:
        proc = subprocess.run(  # nosec B603 - fixed argv, no shell
            [sys.executable, "-I", "-c", _SCRIPT.replace("__POOL_WAIT_S__", str(pool_wait))],
            capture_output=True,
            text=True,
            timeout=process_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            f"the pool probe produced no verdict within {exc.timeout}s (load factor "
            f"{factor:.2f}); a hung terminate is a failure, not a slow host, once the "
            f"budget scales with load. stdout={exc.stdout!r}"
        ) from exc

    assert "LOST" in proc.stdout.splitlines(), (
        "the pool reported the cold-shutdown SystemExit as something other than a lost "
        "worker, so an acks_late task would be ACKED and dropped (billiard 4.3.0 regression). "
        f"stdout={proc.stdout!r} stderr tail={proc.stderr[-400:]!r}"
    )
