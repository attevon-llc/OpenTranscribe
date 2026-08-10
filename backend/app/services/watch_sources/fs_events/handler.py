"""The filesystem event handler fed to watchdog.

Deliberately **not** a ``watchdog.events.FileSystemEventHandler`` subclass:
watchdog only ever calls ``handler.dispatch(event)``, and duck-typing that one
method keeps this module importable where watchdog is absent (the lite image
does not ship it) and unit-testable without starting a real observer.

Responsibilities: answer the delivery probe, drop the noise (directory events,
deletions, partial-download temp files, extensions the source filters out), and
hand everything else to the debouncing dispatcher.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from collections.abc import Sequence

from app.services.watch_sources.fs_events.detection import PROBE_PREFIX

logger = logging.getLogger(__name__)

# Events that can never mean "new media landed".
IGNORED_EVENT_TYPES = frozenset({"deleted", "opened", "closed_no_write"})

# In-progress writes from browsers, rsync, and download managers. The final
# rename into place produces its own event, which is the one we act on.
IGNORED_SUFFIXES = (
    ".part",
    ".partial",
    ".filepart",
    ".crdownload",
    ".download",
    ".tmp",
    ".temp",
    ".swp",
    "~",
)


class WatchEventHandler:
    """Filters raw watchdog events down to "this source may have new media"."""

    def __init__(
        self,
        source_id: int,
        extensions: Sequence[str] | None,
        on_event: Callable[[int], None],
    ) -> None:
        self.source_id = int(source_id)
        self._extensions = tuple(extensions) if extensions else None
        self._on_event = on_event
        self._probe_name: str | None = None
        self._probe_event = threading.Event()

    # ----- delivery probe ------------------------------------------------ #
    def arm_probe(self, filename: str) -> threading.Event:
        """Watch for ``filename`` and return the event set when it is seen."""
        self._probe_event = threading.Event()
        self._probe_name = filename
        return self._probe_event

    def disarm_probe(self) -> None:
        self._probe_name = None

    # ----- watchdog entry point ------------------------------------------ #
    def dispatch(self, event: object) -> None:
        """Called by watchdog for every raw event on the watched tree."""
        try:
            self._dispatch(event)
        except Exception as e:  # noqa: BLE001 - an emitter thread must not die
            logger.debug("Ignoring malformed FS event for source %s: %s", self.source_id, e)

    def _dispatch(self, event: object) -> None:
        paths = [
            _as_text(getattr(event, "src_path", "")),
            _as_text(getattr(event, "dest_path", "")),
        ]

        # The probe check comes first: probe files are dot-prefixed and would
        # otherwise be filtered out before they could confirm delivery.
        probe = self._probe_name
        if probe and any(os.path.basename(p) == probe for p in paths if p):
            self._probe_event.set()
            return

        if getattr(event, "is_directory", False):
            return
        if getattr(event, "event_type", "") in IGNORED_EVENT_TYPES:
            return
        # For a rename, only the destination name matters.
        candidate = next((p for p in reversed(paths) if p), "")
        if not candidate or not self.is_interesting(os.path.basename(candidate)):
            return
        self._on_event(self.source_id)

    def is_interesting(self, filename: str) -> bool:
        """True when ``filename`` could plausibly be importable media."""
        lowered = filename.lower()
        if not lowered or lowered.startswith((".", PROBE_PREFIX)):
            return False
        if lowered.endswith(IGNORED_SUFFIXES):
            return False
        if self._extensions is None:
            return True
        return any(lowered.endswith(ext) for ext in self._extensions)


def _as_text(value: object) -> str:
    """Normalize watchdog paths (they are bytes when the watch path is bytes)."""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value if isinstance(value, str) else ""
