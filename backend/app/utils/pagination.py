"""Shared offset/limit pagination helpers.

Consolidates the ``total = query.count(); rows = query.offset(skip).limit(limit).all()``
boilerplate repeated across list endpoints. Behavior-preserving: ``paginate``
computes the total with ordering stripped (``order_by(None)`` — ordering never
affects a COUNT and lets Postgres skip an unnecessary sort) and then returns the
ordered/sliced rows, exactly as the hand-rolled sites did.
"""

from typing import Annotated
from typing import Any

from fastapi import Depends
from fastapi import Query
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


class PageParams:
    """Reusable skip/limit query-parameter dependency.

    Usage::

        @router.get("/things")
        def list_things(page: PageParams = Depends()): ...
    """

    def __init__(
        self,
        skip: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ) -> None:
        self.skip = skip
        self.limit = limit


PageParamsDep = Annotated[PageParams, Depends()]
