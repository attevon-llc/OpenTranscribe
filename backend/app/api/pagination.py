"""Reusable skip/limit query-parameter dependency for list endpoints.

The query-side helper is ``app.utils.pagination.paginate``.
"""

from typing import Annotated

from fastapi import Depends
from fastapi import Query


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
