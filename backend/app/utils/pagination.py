"""Offset/limit pagination for SQLAlchemy queries.

Consolidates the ``total = query.count(); rows = query.offset(skip).limit(limit).all()``
boilerplate repeated across list queries. ``paginate`` computes the total with ordering
stripped (``order_by(None)``: ordering never affects a COUNT and lets Postgres skip an
unnecessary sort) and then returns the ordered/sliced rows. The FastAPI query-parameter
dependency lives in ``app.api.pagination``.
"""

from typing import Any

from sqlalchemy.orm import Query as SAQuery


def paginate(query: SAQuery, skip: int = 0, limit: int = 100) -> tuple[list[Any], int]:
    """Return ``(rows, total)`` for an offset/limit slice of ``query``.

    Args:
        query: A SQLAlchemy ``Query`` (may already carry an ``order_by``; it is
            preserved on the returned rows and stripped only for the count).
        skip: Number of rows to skip (offset).
        limit: Maximum number of rows to return.

    Returns:
        Tuple of (list of rows for this page, total row count before slicing).
    """
    total = query.order_by(None).count()
    rows = query.offset(skip).limit(limit).all()
    return rows, total
