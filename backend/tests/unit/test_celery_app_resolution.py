"""Celery task/app resolution must not depend on which thread dispatches (issue #485).

Every admin "Run now" endpoint 500'd intermittently because ``@shared_task`` resolves its
task through ``celery._state.get_current_app()``, which reads a **thread-local**.
``Celery.__init__`` sets only the creating thread's slot and never the module-global
``default_app``, so on a Starlette threadpool worker thread both were ``None`` and Celery
minted a fallback ``Celery('default')`` with **no broker** — dispatching to the
``amqp://guest@localhost:5672//`` class default, which nothing listens on here.

Why these tests look the way they do:

- **They assert on APP IDENTITY, not on the endpoint response.** The autouse
  ``_skip_celery_dispatch`` fixture (``tests/conftest.py``) patches
  ``celery.app.task.Task.apply_async`` at the *class* level, so a route test asserting
  ``task_id == "test-task-id"`` passes whichever app the proxy resolved. It cannot see this
  bug at all.
- **The thread cases run in a clean process.** ``default_app`` is process-global and, once
  minted, is cached forever — so a single earlier import in the same interpreter permanently
  masks the failure for every later test. Testing this in-process would be a test that
  cannot fail.
"""

from __future__ import annotations

import pytest

# Kept out of the parallel pool: each case forks an interpreter that imports the whole app.
pytestmark = pytest.mark.xdist_group("celery_app_resolution")


_THREAD_PROBE = """
import threading
from app.core.celery import celery_app
from app.tasks.backup_tasks import run_backup

result = {}


def probe():
    task = run_backup
    result["same_app"] = task.app is celery_app
    result["broker"] = str(task.app.conf.broker_url)


t = threading.Thread(target=probe)
t.start()
t.join()
print(f'{result["same_app"]}|{result["broker"]}')
"""


_THREADPOOL_PROBE = """
import anyio
from starlette.concurrency import run_in_threadpool
from app.core.celery import celery_app
from app.tasks.backup_tasks import run_backup


async def main():
    def dispatch_thread_view():
        return (run_backup.app is celery_app, str(run_backup.app.conf.broker_url))

    same_app, broker = await run_in_threadpool(dispatch_thread_view)
    print(f"{same_app}|{broker}")


anyio.run(main)
"""


_DEFAULT_APP_PROBE = """
from app.core.celery import celery_app
from celery import _state

print(_state.default_app is celery_app)
"""


def test_default_app_is_the_configured_app(run_in_clean_process):
    """Importing the app must populate ``_state.default_app``, not just this thread's slot.

    This is the single line that fixes the whole class: every ``get_current_app()`` fallback
    resolves through ``default_app``.
    """
    assert run_in_clean_process(_DEFAULT_APP_PROBE) == "True"


def test_task_resolves_the_configured_app_from_a_plain_worker_thread(run_in_clean_process):
    """A thread that did not import the app still resolves the real, Redis-backed app."""
    same_app, broker = run_in_clean_process(_THREAD_PROBE).split("|")

    assert same_app == "True", "task resolved a different app object on a non-import thread"
    assert broker.startswith("redis"), (
        f"non-import thread resolved broker {broker!r}; 'None' means the phantom "
        "Celery('default') came back and dispatch would go to amqp://"
    )


def test_task_resolves_the_configured_app_under_run_in_threadpool(run_in_clean_process):
    """The exact call shape of a sync ``def`` FastAPI endpoint.

    ``run_in_threadpool`` is how Starlette runs every sync endpoint handler, and is what the
    three admin "Run now" routes go through. A bare ``threading.Thread`` reproduces the same
    mechanism, but only this one matches production.
    """
    same_app, broker = run_in_clean_process(_THREADPOOL_PROBE).split("|")

    assert same_app == "True"
    assert broker.startswith("redis"), f"threadpool worker resolved broker {broker!r}"


# ---------------------------------------------------------------------------
# Task names are a wire contract — beat and routing reference them as strings
# ---------------------------------------------------------------------------

#: The 14 tasks converted from ``@shared_task`` to ``@celery_app.task`` for #485. All 14
#: already declared an explicit ``name=``, so the decorator swap cannot rename them — this
#: pins that, because a silent rename breaks ``beat_schedule`` with no error at all: beat
#: would keep publishing a name no worker has registered.
CONVERTED_TASK_NAMES = frozenset(
    {
        "backup.check_schedule",
        "backup.run",
        "backup.mirror_check_schedule",
        "backup.mirror_run",
        "directory.sync_check_schedule",
        "directory.sync_run",
        "cleanup.run_periodic_cleanup",
        "cleanup.deep_cleanup",
        "cleanup.health_check",
        "cleanup.emergency_recovery",
        "cleanup.orphan_upload_sweeper",
        "cleanup.scratch_janitor",
        "chat.retention_sweep",
        "gdpr.erasure_reconcile",
    }
)


@pytest.fixture(scope="module")
def registered_task_names() -> frozenset[str]:
    """Every task name the app knows, with the lazy ``include=`` modules imported."""
    from app.core.celery import celery_app

    celery_app.loader.import_default_modules()
    celery_app.finalize()
    return frozenset(celery_app.tasks.keys())


def test_every_converted_task_is_still_registered_under_its_original_name(
    registered_task_names,
):
    missing = sorted(CONVERTED_TASK_NAMES - registered_task_names)

    assert not missing, (
        f"these task names disappeared from the registry: {missing}. "
        "beat_schedule and task_routes address tasks by string name, so a rename "
        "silently stops the schedule rather than raising."
    )


def test_every_beat_schedule_entry_points_at_a_registered_task(registered_task_names):
    from app.core.celery import celery_app

    schedule = celery_app.conf.beat_schedule
    assert schedule, "beat_schedule is empty — this test would assert nothing"

    unresolvable = sorted(
        {entry["task"] for entry in schedule.values() if entry["task"] not in registered_task_names}
    )

    assert not unresolvable, f"beat entries naming no registered task: {unresolvable}"


def test_every_task_route_points_at_a_registered_task(registered_task_names):
    from app.core.celery import celery_app

    routes = celery_app.conf.task_routes
    assert routes, "task_routes is empty — this test would assert nothing"

    # Routing keys may be glob patterns ("app.tasks.*"); only exact names are checkable.
    exact = {key for key in routes if not any(ch in key for ch in "*?[")}
    assert exact, "no exact-match routing keys — this test would assert nothing"

    unresolvable = sorted(exact - registered_task_names)

    assert not unresolvable, f"task_routes keys naming no registered task: {unresolvable}"
