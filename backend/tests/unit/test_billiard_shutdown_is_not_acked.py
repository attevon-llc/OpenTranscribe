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

import subprocess  # nosec B404 - runs this interpreter on a fixed script
import sys
import textwrap

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
            outcome = pool.apply_async(job).get(timeout=150)
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


# The budgets are for a 48-way xdist run on a loaded host (release.sh test timed out at 90 s with
# load ~60; the test takes ~12 s alone). A regression still fails fast: the pool returns RESULT.
def test_the_shutdown_systemexit_is_reported_as_a_lost_worker_not_a_result():
    proc = subprocess.run(  # nosec B603 - fixed argv, no shell
        [sys.executable, "-I", "-c", _SCRIPT],
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )

    assert "LOST" in proc.stdout.splitlines(), (
        "the pool reported the cold-shutdown SystemExit as something other than a lost "
        "worker, so an acks_late task would be ACKED and dropped (billiard 4.3.0 regression). "
        f"stdout={proc.stdout!r} stderr tail={proc.stderr[-400:]!r}"
    )
