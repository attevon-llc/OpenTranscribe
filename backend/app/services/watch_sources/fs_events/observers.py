"""Thin, lazily-imported wrappers around watchdog's observer classes.

``watchdog`` is imported **inside** these functions on purpose: it is absent
from ``requirements-lite.txt``, and the rest of the FS-event package must stay
importable (and unit-testable) without it. Nothing else in the package touches
``watchdog`` directly, which also makes the whole selection path trivial to fake
in tests.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def watchdog_available() -> bool:
    """True when the watchdog package can be imported in this image."""
    try:
        import watchdog.observers  # noqa: F401
    except Exception as e:  # noqa: BLE001 - missing/broken dep must not raise
        logger.debug("watchdog is not available: %s", e)
        return False
    return True


def native_observer() -> Any:
    """The platform observer — ``InotifyObserver`` in our Linux containers."""
    from watchdog.observers import Observer

    return Observer()


def polling_observer(timeout: float) -> Any:
    """Watchdog's stat-sweep observer: slower, but works on every filesystem."""
    from watchdog.observers.polling import PollingObserver

    return PollingObserver(timeout=timeout)


def stop_observer(observer: Any, timeout: float = 5.0) -> None:
    """Stop and join an observer, swallowing anything it throws on the way out."""
    if observer is None:
        return
    try:
        observer.stop()
    except Exception as e:  # noqa: BLE001 - shutdown is best-effort
        logger.debug("Error stopping FS observer: %s", e)
    try:
        observer.join(timeout)
    except Exception as e:  # noqa: BLE001 - shutdown is best-effort
        logger.debug("Error joining FS observer: %s", e)
