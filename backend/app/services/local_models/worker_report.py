"""Which Whisper model each worker has actually loaded (#1246).

``/asr-settings/local-model/set`` only writes the database; a running worker keeps the model it
pinned at start. After ``/restart`` the Settings page needs to know when the new model is
really serving, and the only truthful source is the worker itself. So a Whisper worker records
what it loaded, in Redis, the moment it has pinned it (``core/celery.py:preload_models``), and
``GET /asr-settings/local-model/active`` reads those records.

Activation semantics, verified against celery 5.6.3 (see ``tests`` and the plan):

* ``/set`` changes the stored choice; nothing running changes.
* ``/restart`` sends a remote *warm* shutdown to the Whisper workers (``WHISPER_WORKER_PREFIXES``
  in ``core/constants.py``). Each stops consuming, finishes the jobs it is running (the remote
  control does not arm the cooperative abort flag, so nothing is cut short), hands unstarted
  prefetched messages back to the queue, exits, is restarted by Docker, loads the new model in
  ``preload_models`` and records it here. Jobs submitted meanwhile queue and run on the new model.
* A client confirms a switch by waiting for every worker NAMED in the restart response to appear
  here with ``ready_at`` after ``restart_requested_at``. Keys of workers that no longer exist
  (a recreated container has a new hostname) expire after a week and must never be waited on.

The Redis client is the time-bounded probe client, not the shared one: the write happens on
``worker_ready`` in the MainProcess main thread, where a stalled call would freeze the event loop
(see ``preload_models``), and the shared client has no socket timeout.

Redis failures are logged and swallowed on write (a worker must start whatever Redis does) and
read as "no reports" on read (the page then shows the stored choice only).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

KEY_PREFIX = "asr:worker_model:"
REPORT_TTL_SECONDS = 7 * 24 * 3600
SCAN_COUNT = 100


def report_loaded_model(hostname: str, model_name: str, redis_client: Any = None) -> None:
    """Record that ``hostname`` has loaded ``model_name``. Never raises.

    Args:
        hostname: The worker's full Celery node name, e.g. ``gpu-transcription@abc123``.
        model_name: The model the worker pinned.
        redis_client: Override for tests; defaults to the time-bounded application Redis client.
    """
    payload = json.dumps({"model": model_name, "ready_at": datetime.now(UTC).isoformat()})
    try:
        if redis_client is None:
            from app.core.redis import get_probe_redis

            redis_client = get_probe_redis()
        redis_client.set(f"{KEY_PREFIX}{hostname}", payload, ex=REPORT_TTL_SECONDS)
    except Exception as exc:
        logger.warning("Could not record the loaded model: %s", type(exc).__name__)


def read_loaded_models(redis_client: Any = None) -> list[dict[str, str]]:
    """Return ``[{"worker", "model", "ready_at"}]`` for every recorded worker, or ``[]``.

    A Redis failure or an unreadable record yields fewer entries, never an exception: the
    caller is a settings page that must keep working without them.
    """
    try:
        if redis_client is None:
            from app.core.redis import get_probe_redis

            redis_client = get_probe_redis()
        keys = sorted(redis_client.scan_iter(match=f"{KEY_PREFIX}*", count=SCAN_COUNT))
        reports: list[dict[str, str]] = []
        for key in keys:
            raw = redis_client.get(key)
            if raw is None:
                continue
            name = key.decode("utf-8") if isinstance(key, bytes) else str(key)
            try:
                record = json.loads(raw)
                reports.append(
                    {
                        "worker": name.removeprefix(KEY_PREFIX),
                        "model": str(record["model"]),
                        "ready_at": str(record["ready_at"]),
                    }
                )
            except (ValueError, KeyError, TypeError):
                logger.warning("Ignoring an unreadable worker model record")
        return reports
    except Exception as exc:
        logger.warning("Could not read the loaded models: %s", type(exc).__name__)
        return []
