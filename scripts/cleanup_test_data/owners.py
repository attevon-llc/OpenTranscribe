"""Test-owner discovery and the deletion of owners and candidates."""

from __future__ import annotations

from types import ModuleType
from typing import Any

from sqlalchemy import text

from cleanup_test_data.api import ApiSession
from cleanup_test_data.finders import Candidate
from cleanup_test_data.settings import _load_cleanup_users_module

# ---------------------------------------------------------------------------------------------
# Owner deletion (issue #1237)
# ---------------------------------------------------------------------------------------------


def owner_patterns(users_module: ModuleType, *, include_tier_b: bool) -> list[str]:
    """Registered test-user email patterns whose accounts may be deleted: Tier A always,
    the review tier only under the full ``--execute``."""
    return list(
        users_module.ORPHAN_PATTERNS if include_tier_b else users_module.ORPHAN_PATTERNS_UNAMBIGUOUS
    )


def find_deletable_test_owners(conn: Any, cutoff: int, patterns: list[str]) -> dict[str, str]:
    """``{email: uuid}`` of registered test users created before the liveness cutoff.

    The admin is not authorised on another user's rows (a speaker profile is a 403), so the
    only delete that reaches them is the owner's own, which cascades. An account is only a
    candidate here when its email FULLY matches a registered test shape, it predates every
    live run, and it is not one of ``cleanup-test-users.py``'s keep-list accounts.
    """
    if not patterns:
        return {}
    users_module = _load_cleanup_users_module()
    where = ' OR '.join(f"u.email LIKE '{p}' ESCAPE '\\'" for p in patterns)
    rows = conn.execute(
        text(
            f"""
            SELECT u.uuid, u.email
            FROM "user" u
            WHERE ({where})
              AND u.created_at < to_timestamp(:cutoff)
            """
        ),
        {'cutoff': cutoff},
    ).fetchall()
    return {
        row.email: str(row.uuid)
        for row in rows
        if row.email not in users_module.KEEP_EMAILS
        and not row.email.startswith(users_module.KEEP_PREFIXES)
    }


def owners_to_delete(
    candidates_by_resource: dict[str, list[Candidate]], deletable: dict[str, str]
) -> dict[str, str]:
    """The ``{email: uuid}`` owners that at least one candidate row belongs to."""
    return {
        cand.owner_email: deletable[cand.owner_email]
        for candidates in candidates_by_resource.values()
        for cand in candidates
        if cand.owner_email in deletable
    }


# ---------------------------------------------------------------------------------------------
# Reporting / main
# ---------------------------------------------------------------------------------------------


def _delete_owners(api: ApiSession, owners: dict[str, str]) -> dict[str, bool]:
    """Delete registered test-user accounts (cascading their rows). ``{email: succeeded}``.

    Like ``_delete_candidates``, reached only through the ``effective_execute`` guard.
    """
    outcome: dict[str, bool] = {}
    for email, user_uuid in owners.items():
        outcome[email] = api.delete_user(user_uuid, email)
    return outcome


def _delete_candidates(
    api: ApiSession,
    resource: str,
    candidates: list[Candidate],
    owner_outcome: dict[str, bool],
) -> tuple[int, bool]:
    """Issue the real deletes for one resource type. Returns ``(deleted_count, any_failed)``.

    A candidate whose owner was handled by ``_delete_owners`` is not deleted individually: the
    owner delete removed it (counted deleted) or failed (counted failed, already reported).

    The ONLY place in this module that calls ``api.delete_media_file`` /
    ``api.delete_simple`` / ``api.delete_tags`` — callers must reach this function
    through the ``if effective_execute and api_available:`` guard in ``main()``.
    """
    deleted = 0
    any_failed = False
    for cand in candidates:
        if cand.owner_email in owner_outcome:
            if owner_outcome[cand.owner_email]:
                deleted += 1
            else:
                any_failed = True
    candidates = [c for c in candidates if c.owner_email not in owner_outcome]
    if resource == 'tag':
        if candidates and api.delete_tags([c.uuid for c in candidates]):
            deleted += len(candidates)
        elif candidates:
            any_failed = True
    elif resource == 'media_file':
        for cand in candidates:
            if api.delete_media_file(cand.uuid):
                deleted += 1
            else:
                any_failed = True
    else:
        for cand in candidates:
            if api.delete_simple(resource, cand.uuid):
                deleted += 1
            else:
                any_failed = True
    return deleted, any_failed
