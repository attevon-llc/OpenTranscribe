import contextlib
import logging
from collections.abc import Iterator
from typing import Any

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.base import SessionLocal

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def session_scope() -> Iterator[Session]:
    """Provide a transactional scope around a series of operations.

    Usage:
        with session_scope() as db:
            db_obj = db.query(Model).get(id)
            db_obj.property = new_value
            # No need to call commit - it's done automatically if no exceptions
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception as e:
        logger.error(f"Session error, rolling back: {str(e)}")
        session.rollback()
        raise
    finally:
        session.close()


def _is_mapped(obj: object) -> bool:
    return obj is not None and sa_inspect(obj, raiseerr=False) is not None


class KeptObjectDirtyError(RuntimeError):
    """An object passed to :func:`end_read_transaction` has unflushed changes."""


def end_read_transaction(db: Session, *keep: object) -> None:
    """Commit the current (read) transaction and return its connection to the pool.

    Objects in ``keep`` are expunged first, so the commit does not expire them, then
    re-attached with ``add()`` (no SQL). Their unloaded relationships lazy-load later
    in a new transaction, on the same Session.

    SQLAlchemy keeps a Session's connection checked out for as long as a transaction is
    open, and a Session with no transaction holds none (Session Basics, "When do I
    construct a Session, when do I commit it, and when do I close it?": keep
    transactions short). ``commit()`` expires every object in the Session (State
    Management, "When to Expire or Refresh"); an expunged one is detached and keeps its
    loaded attributes, and ``Session.add()`` makes a detached instance persistent again
    without emitting SQL.

    Commit rather than rollback: anything an earlier step legitimately wrote is kept,
    and in the test harness a commit releases the savepoint instead of discarding the
    rows the test seeded.

    Args:
        db: The request's Session. Anything that is not a ``Session`` holds no pooled
            connection, so there is nothing to release and the call does nothing; the
            auth dependencies are also called directly, with structural stand-ins.
        *keep: Objects the caller goes on using after the transaction ends. Objects not
            in ``db`` (transient, or not mapped at all) are ignored.

    Raises:
        KeptObjectDirtyError: A kept object has unflushed changes. Ending the transaction
            would commit them silently; a caller that means to write must commit itself.
    """
    if not isinstance(db, Session):
        return
    attached = [obj for obj in keep if _is_mapped(obj) and obj in db]
    dirty = [obj for obj in attached if obj in db.dirty]
    if dirty:
        raise KeptObjectDirtyError(
            f"end_read_transaction() asked to keep {len(dirty)} modified object(s): "
            "commit the change explicitly before ending a read transaction"
        )
    for obj in attached:
        db.expunge(obj)
    try:
        db.commit()
    finally:
        for obj in attached:
            db.add(obj)


def get_refreshed_object(db: Session, model_class: type[Any], obj_id: int) -> Any:
    """Get a fresh copy of an object from the database.

    Useful for celery tasks that need to get a fresh instance of a model
    after it's been detached from a previous session.
    """
    try:
        # First try with the provided session
        return db.query(model_class).filter(model_class.id == obj_id).first()
    except SQLAlchemyError as e:
        logger.warning(f"Session error while getting object, creating new session: {e}")
        # If that fails, create a new session as a fallback
        temp_session = SessionLocal()
        try:
            return temp_session.query(model_class).filter(model_class.id == obj_id).first()
        finally:
            temp_session.close()
