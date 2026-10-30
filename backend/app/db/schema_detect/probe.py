"""The one query primitive every schema-detection probe uses."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection


def check_exists(conn: Connection, query: str) -> bool:
    """Run one ``SELECT EXISTS(...)`` probe and return its answer as a bool."""
    return bool(conn.execute(text(query)).scalar())
