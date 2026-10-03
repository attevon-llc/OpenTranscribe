"""A real Celery app for ``test_orphaned_delivery_recovery_live.py`` -- run as a worker subprocess.

It mirrors the production broker contract exactly where that contract matters to the test:
the SAME ``broker_transport_options`` object as ``app.core.celery`` (priority steps, queue
order strategy, the six-hour visibility timeout), the same prefetch, the same shutdown
settings, and a task registered under a real transcription-stage NAME with the same
``acks_late=True, reject_on_worker_lost=True`` declaration. Its body holds a real run lease
(``app.core.task_liveness.run_heartbeat``), exactly like a pipeline stage, and then sleeps --
nothing here touches a GPU, a database or object storage.

Configuration comes from the environment the test sets: ``ORPHAN_TEST_BROKER_URL`` (the
broker and result backend) and ``REDIS_URL`` (where the lease is written; the same Redis).
"""

from __future__ import annotations

import os

import redis
from celery import Celery
from kombu import Queue

from app.core.celery import celery_app as production_app
from app.core.task_liveness import run_heartbeat

BROKER_URL = os.environ["ORPHAN_TEST_BROKER_URL"]
STARTED_KEY = "orphan-test:started"
DONE_KEY = "orphan-test:done"
#: While this key exists the stage keeps running (and keeps its lease), so the test decides
#: when a run is "long" without baking a duration into the message itself. The test ends the
#: hold by deleting it and pushing to RELEASE_KEY, which the stage blocks on.
BLOCK_KEY = "orphan-test:block:{task_id}"
RELEASE_KEY = "orphan-test:release:{task_id}"

app = Celery("orphan_test", broker=BROKER_URL, backend=BROKER_URL)
_prod = production_app.conf
app.conf.update(
    broker_transport_options=dict(_prod.broker_transport_options),
    task_queues=(Queue("cpu"),),
    task_default_queue="cpu",
    task_create_missing_queues=False,
    worker_prefetch_multiplier=_prod.worker_prefetch_multiplier,
    worker_soft_shutdown_timeout=float(os.getenv("ORPHAN_TEST_SOFT_SHUTDOWN", "0")),
    worker_enable_soft_shutdown_on_idle=_prod.worker_enable_soft_shutdown_on_idle,
    worker_cancel_long_running_tasks_on_connection_loss=False,
    worker_deduplicate_successful_tasks=_prod.worker_deduplicate_successful_tasks,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    worker_hijack_root_logger=False,
)


@app.task(
    name="transcription.preprocess",
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
)
def stage(self, file_uuid: str, task_id: str) -> str:
    client = redis.from_url(BROKER_URL)
    with run_heartbeat(task_id):
        client.rpush(STARTED_KEY, task_id)
        if client.exists(BLOCK_KEY.format(task_id=task_id)):
            client.blpop([RELEASE_KEY.format(task_id=task_id)], timeout=600)
    client.rpush(DONE_KEY, task_id)
    return task_id
