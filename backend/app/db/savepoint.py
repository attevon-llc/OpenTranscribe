"""Run a database read inside a SAVEPOINT so a failure cannot poison the caller's transaction."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session


def isolated[T](db: Session, read: Callable[[], T]) -> T:
    """Run ``read`` inside a savepoint; a failure rolls back only the savepoint, then re-raises.

    Written with an explicit ``rollback``/``commit`` rather than ``with db.begin_nested()``:
    the context-manager form raises "Can't operate on closed transaction" under the test
    harness's savepoint-restarting listener once the test has committed.
    """
    savepoint = db.begin_nested()
    try:
        result = read()
    except BaseException:
        savepoint.rollback()
        raise
    savepoint.commit()
    return result
