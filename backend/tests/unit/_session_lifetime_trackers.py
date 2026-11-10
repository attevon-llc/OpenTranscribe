# mypy: disable-error-code="arg-type"
# This suite passes structural stand-ins (a fake session, a recording Path) to
# signatures that declare Session/Path. Declared once here rather than as a cast at
# every call site: a cast buries the thing being asserted, and widening a production
# signature to suit a test is worse. Same convention as
# tests/unit/test_proxy_identity_consistency.py.
"""Shared stand-ins for the session-lifetime tests (Celery tasks and API handlers).

The tests swap a module's ``session_scope`` for ``ScopeTracker`` (a depth-tracking
wrapper over the savepointed ``db_session``) and have each stub for a slow call
report the open-scope depth at the moment it runs.
"""

import contextlib
import uuid as uuid_mod
from typing import Any

from tests.fixtures.speaker_attribute_tracking import add_file_with_speakers


class ScopeTracker:
    """Stands in for ``session_scope``, recording how many scopes are open."""

    def __init__(self, session):
        self._session = session
        self.depth = 0
        self.max_depth = 0
        self.opened = 0
        #: (label, depth) reported from inside each slow call.
        self.observations: list[tuple[str, int]] = []
        #: (label, depth, scopes-opened-so-far) for the same calls.
        self.timeline: list[tuple[str, int, int]] = []
        #: Per-task payloads the fixtures hang here so a test can assert on what the
        #: slow phase actually received (the aggregated vector, the indexed uuids, ...).
        #: Declared rather than bolted on dynamically so mypy can see them.
        self.recorded: Any = None
        self.indexed: Any = None

    @contextlib.contextmanager
    def scope(self):
        self.depth += 1
        self.opened += 1
        self.max_depth = max(self.max_depth, self.depth)
        try:
            yield self._session
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        finally:
            self.depth -= 1

    def observe(self, label: str) -> None:
        self.observations.append((label, self.depth))
        #: The scope COUNT at the moment of the call. Two observations sharing a
        #: count ran inside the same scope; different counts prove they did not.
        #: Depth alone cannot tell those apart.
        self.timeline.append((label, self.depth, self.opened))

    def opened_at(self, label: str) -> list[int]:
        """Scope counts at each occurrence of ``label``."""
        return [count for name, _depth, count in self.timeline if name == label]

    @property
    def seen(self) -> dict[str, int]:
        return dict(self.observations)


def leak(tracker: ScopeTracker, label: str) -> str:
    return f"a DB transaction was held across the slow phase ({label}): {tracker.observations}"


def make_transcribed_file(db_session, user, *, speakers: int = 2):
    """A media file with speakers and segments, as after a completed transcription."""
    return add_file_with_speakers(
        db_session,
        user,
        speakers=speakers,
        seg_seconds=4.0,
        filename="meeting.mp4",
        storage_path=f"test/meeting-{uuid_mod.uuid4().hex}.mp4",
    )


#: Session methods that (re)open a transaction on first use.
_DB_WORK = frozenset(
    {
        "query",
        "execute",
        "add",
        "add_all",
        "flush",
        "refresh",
        "get",
        "scalar",
        "scalars",
        "merge",
        "delete",
        "bulk_save_objects",
    }
)

#: Session methods that END the current transaction and release the connection.
_DB_RELEASE = frozenset({"commit", "rollback", "close"})


class TrackedRequestSession:
    """Stands in for the request-scoped ``Depends(get_db)`` session.

    Records whether a Postgres transaction is open at the moment each network stub
    runs, and how many separate transactions the handler opened.

    ``close()`` is deliberately **not** forwarded: the ``db_session`` fixture is
    savepoint-isolated inside the test's own transaction, and a real close would
    unwind it. It is translated to ``expunge_all()``, which reproduces the part
    that matters — every ORM instance the handler loaded becomes **detached**,
    exactly as after ``get_db``'s session is released — so a handler that kept
    reading an ORM row past the release fails here rather than silently working.
    """

    def __init__(self, session):
        self._session = session
        #: Is a transaction currently open?
        self.open = False
        #: How many distinct transactions were opened.
        self.opened = 0
        #: (label, transaction-open?) reported from inside each network call.
        self.observations: list[tuple[str, bool]] = []

    def observe(self, label: str) -> None:
        self.observations.append((label, self.open))

    @property
    def seen(self) -> dict[str, bool]:
        return dict(self.observations)

    def __getattr__(self, name: str):
        attr = getattr(self._session, name)
        if name in _DB_WORK:

            def _work(*args, **kwargs):
                if not self.open:
                    self.open = True
                    self.opened += 1
                return attr(*args, **kwargs)

            return _work
        if name in _DB_RELEASE:

            def _release(*args, **kwargs):
                self.open = False
                if name == "close":
                    # See the class docstring — never really close the harness session.
                    return self._session.expunge_all()
                return attr(*args, **kwargs)

            return _release
        return attr
