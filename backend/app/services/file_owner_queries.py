"""Owner lookups over a file scope."""

from __future__ import annotations

from sqlalchemy import ColumnElement
from sqlalchemy import Select
from sqlalchemy import select

from app.models.media import MediaFile


def distinct_owner_ids(scope: ColumnElement[bool]) -> Select[tuple[int]]:
    """``SELECT DISTINCT user_id`` of the files satisfying ``scope``, for ``User.id.in_(...)``.

    Joining every file in scope to ``user`` and de-duplicating the joined rows sorted all of
    them; picking the owner ids first and reading only those users is a semi-join (#1337 G11).
    """
    return select(MediaFile.user_id).where(scope).distinct()
