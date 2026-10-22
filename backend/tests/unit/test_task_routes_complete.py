"""Every registered Celery task has a ``task_routes`` entry or a written reason (#1268).

A task with no route lands on the default ``celery`` queue whenever its caller does not
pass ``queue=`` itself. Five tasks shipped that way and were reported only by a worker
startup log line (``_validate_task_routes``) on a cloud deployment. This test turns that
log line into a failure, using the same ``unrouted_task_names()`` the worker calls, so
the runtime check and the test cannot disagree.

It also forbids a decorator-level ``queue=``: Celery merges the route UNDER the task's
own options (``celery.app.routes.Router.route`` -> ``lpmerge(route, options)``), so a
decorator queue silently overrides ``task_routes`` and the routing table stops being the
single source of truth it is documented as.
"""

from __future__ import annotations

from app.core.celery import INTENTIONALLY_UNROUTED_TASKS
from app.core.celery import celery_app
from app.core.celery import unrouted_task_names
from app.core.constants import CeleryQueues

# The five tasks issue #1268 found unrouted, with the queue each must reach.
_ISSUE_1268_ROUTES = {
    "chat.retention_sweep": CeleryQueues.UTILITY,
    "pipeline_timing.flush_tail": CeleryQueues.UTILITY,
    "recovery.youtube_metadata_fetch": CeleryQueues.CPU,
    "recovery.youtube_metadata_backfill": CeleryQueues.CPU,
    "recovery.media_duration_backfill": CeleryQueues.CPU,
}


def _registered_app_tasks() -> set[str]:
    celery_app.loader.import_default_modules()  # register every module in `include=`
    return {name for name in celery_app.tasks if not name.startswith("celery.")}


def test_registry_is_populated_so_the_route_check_is_not_vacuous():
    registered = _registered_app_tasks()
    # An empty or half-imported registry would make the completeness check pass on nothing.
    assert len(registered) >= 90, f"only {len(registered)} tasks registered"
    assert set(_ISSUE_1268_ROUTES) <= registered
    assert set(INTENTIONALLY_UNROUTED_TASKS) <= registered, (
        "an allowlist entry names a task that no longer exists — delete it"
    )


def test_every_registered_task_has_a_route_or_a_written_reason():
    _registered_app_tasks()
    assert unrouted_task_names() == [], (
        "these tasks would fall to the default 'celery' queue: add a task_routes entry in "
        "app/core/celery.py, or (call-time routing only) an INTENTIONALLY_UNROUTED_TASKS "
        "entry with its reason"
    )


def test_allowlist_entries_carry_a_reason_and_no_static_route():
    routes = celery_app.conf.task_routes
    assert "transcription.diarize_gpu" in INTENTIONALLY_UNROUTED_TASKS
    for name, reason in INTENTIONALLY_UNROUTED_TASKS.items():
        assert reason.strip(), f"{name} is allowlisted without a reason"
        assert name not in routes, f"{name} is both routed and allowlisted as unrouted"


def test_issue_1268_tasks_route_to_a_queue_every_deployment_drains():
    for name, queue in _ISSUE_1268_ROUTES.items():
        assert celery_app.conf.task_routes[name] == {"queue": queue}, name
        # What a bare .delay()/send_task actually publishes to, not just the table entry.
        options = celery_app.amqp.router.route({}, name)
        assert options["queue"].name == queue, name


def test_no_task_pins_a_queue_in_its_decorator():
    pinned = sorted(
        name
        for name in _registered_app_tasks()
        if getattr(celery_app.tasks[name], "queue", None) is not None
    )
    assert pinned == [], (
        "a decorator queue= overrides task_routes silently; route these in "
        "app/core/celery.py instead"
    )
