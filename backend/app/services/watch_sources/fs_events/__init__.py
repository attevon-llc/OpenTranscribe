"""Event-driven watching for local watch sources (issue #294).

``watch.fs_events_enabled`` used to be a setting with no consumer: the admin
could tick it, tick the per-source box, and still wait up to
``polling_interval_minutes`` (15 by default) with nothing explaining why. This
package is the consumer.

Layout:

- ``supervisor`` — the long-lived reconciler that runs in **celery-beat**;
  ``start_supervisor()`` is called from Celery's ``beat_init`` signal.
- ``detection``  — is this mount capable of delivering native events at all?
  (``/proc/self/mountinfo`` heuristic + a live probe).
- ``observers``  — lazily-imported watchdog observer factories.
- ``handler``    — event filtering and the probe answer.
- ``dispatcher`` — debounce + Redis-locked ``watch_source.scan_single`` dispatch.
- ``status``     — the Redis blob the API reads to tell the UI which mode a
  source actually ended up in.

The Celery poll is never disabled: it stays the safety net, so a total failure
of this layer degrades to exactly the previous behaviour.
"""

from app.services.watch_sources.fs_events.status import MODE_ERROR
from app.services.watch_sources.fs_events.status import MODE_NATIVE
from app.services.watch_sources.fs_events.status import MODE_POLLING
from app.services.watch_sources.fs_events.status import MODE_UNAVAILABLE
from app.services.watch_sources.fs_events.supervisor import FsEventSupervisor
from app.services.watch_sources.fs_events.supervisor import WatchPlan
from app.services.watch_sources.fs_events.supervisor import get_supervisor
from app.services.watch_sources.fs_events.supervisor import start_supervisor
from app.services.watch_sources.fs_events.supervisor import stop_supervisor

__all__ = [
    "MODE_ERROR",
    "MODE_NATIVE",
    "MODE_POLLING",
    "MODE_UNAVAILABLE",
    "FsEventSupervisor",
    "WatchPlan",
    "get_supervisor",
    "start_supervisor",
    "stop_supervisor",
]
