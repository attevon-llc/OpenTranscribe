"""Does the database admit a registry-based provider's ``auth_type``? (issue #866)

``external_sync`` writes ``identity.provider`` straight into ``user.auth_type``, which
``ck_user_auth_type_valid`` constrains to ``constants.DB_ADMITTED_AUTH_TYPES``. A provider
registered through ``app.auth.provider_registry`` under any other string is therefore
refused by Postgres at commit with a bare ``CheckViolation`` that names a constraint
appearing nowhere in the edition's code. The registering edition must widen both CHECKs
with its own additive migration; this module turns the missing migration into an error
that says so, and costs nothing for the core auth types.

The live constraint is consulted, not just the core constant, because an edition that
did ship that migration is admitted by the database while core cannot know it. Only
positive answers are cached: a refusal is re-checked, so applying the migration takes
effect without a restart.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth.constants import DB_ADMITTED_AUTH_TYPES
from app.core.exceptions import ExternalProviderNotAdmittedError

logger = logging.getLogger(__name__)

_CONSTRAINT = "ck_user_auth_type_valid"
_LITERAL = re.compile(r"'([^']+)'")

#: Values the live constraint is known to admit (positive answers only).
_confirmed: set[str] = set(DB_ADMITTED_AUTH_TYPES)


def core_admits(auth_type: str) -> bool:
    """True when core's own migrations admit ``auth_type`` (no database round trip)."""
    return auth_type in DB_ADMITTED_AUTH_TYPES


def _live_admitted_values(db: Session) -> set[str] | None:
    """Values in the live ``ck_user_auth_type_valid`` body, or None when unreadable."""
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return None
    definition = db.execute(
        text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :name"),
        {"name": _CONSTRAINT},
    ).scalar()
    if not definition:
        return None
    return set(_LITERAL.findall(str(definition)))


def assert_auth_type_admitted(db: Session, auth_type: str) -> None:
    """Raise :class:`ExternalProviderNotAdmittedError` unless the database admits it.

    A database with no readable constraint (non-Postgres, or the CHECK absent) is not
    a reason to refuse a login, so an unreadable constraint passes.
    """
    if auth_type in _confirmed:
        return
    live = _live_admitted_values(db)
    if live is None or auth_type in live:
        _confirmed.add(auth_type)
        return
    raise ExternalProviderNotAdmittedError(
        f"auth_type {auth_type!r} is not admitted by {_CONSTRAINT}. The edition that "
        "registered this provider must widen that constraint and "
        "ck_user_invitation_auth_type_valid with its own additive migration "
        f"(core admits {', '.join(DB_ADMITTED_AUTH_TYPES)})."
    )
