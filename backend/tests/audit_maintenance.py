"""Let a test deliberately tamper with audit rows, the way a migration or restore would.

The audit tables refuse UPDATE of an immutable column and DELETE (migration v453). Some tests
need to simulate what the application cannot do on purpose: time-travel a timestamp, or make
the database "lose" a row so a restore path can be exercised. The table owner may do that under
``opentranscribe.audit_maintenance``; the application role never can. Keep the window as small
as the tampering itself.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text

_SET = text("SELECT set_config('opentranscribe.audit_maintenance', :v, true)")


@contextmanager
def audit_maintenance(session) -> Iterator[None]:
    """Run the body with the owner-only audit maintenance switch on, then switch it off."""
    session.execute(_SET, {"v": "on"})
    yield
    # Only on a normal exit: after an exception the transaction may be aborted, and the setting
    # is transaction-local so the caller's rollback discards it anyway.
    session.execute(_SET, {"v": "off"})
