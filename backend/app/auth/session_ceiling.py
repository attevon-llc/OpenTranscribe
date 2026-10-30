"""Session lifetime configuration and the concurrent-session ceiling.

``_session_lifetime_minutes`` resolves the idle timeout, absolute timeout and
concurrent-session cap from the DB-backed auth settings. ``enforce_session_ceiling``
is the single-statement eviction that keeps a user's newest ``limit`` sessions and
revokes the rest. Both are read by the refresh-token lifecycle in
``auth/refresh_tokens.py``; the ceiling is also applied by the interactive login and
the periodic sweep in ``services/session_cap_service.py``.
"""

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings

logger = logging.getLogger(__name__)


def _session_lifetime_minutes(db: Session) -> tuple[int, int, int]:
    """Return ``(idle_timeout_minutes, absolute_timeout_minutes, max_concurrent_sessions)``.

    DB-backed (admin UI) over ``.env`` over the coded default, like the rest of the
    auth configuration. Never raises: a configuration read must not be able to
    break token issue or verification, so an unavailable database degrades to the
    environment values.

    The concurrent-session cap rides along with the two timeouts precisely so that
    :meth:`TokenService.create_refresh_token` — the one place that needs all three —
    resolves ``get_auth_settings(db)`` exactly once per call, not twice.
    """
    try:
        from app.core.auth_settings import get_auth_settings

        auth_settings = get_auth_settings(db)
        return (
            auth_settings.session_idle_timeout_minutes,
            auth_settings.session_absolute_timeout_minutes,
            auth_settings.max_concurrent_sessions,
        )
    except Exception:  # pragma: no cover - defensive
        logger.debug("Could not read session lifetime config; using .env values", exc_info=True)
        return (
            settings.SESSION_IDLE_TIMEOUT_MINUTES,
            settings.SESSION_ABSOLUTE_TIMEOUT_MINUTES,
            settings.MAX_CONCURRENT_SESSIONS,
        )


def enforce_session_ceiling(
    db: Session, user_id: int, limit: int, *, batch_limit: int | None = None
) -> list[str]:
    """Revoke every active session of *user_id* beyond the newest *limit*.

    Expressed as a **post-condition** ("keep the newest ``limit``, revoke
    everything else"), not as an action ("evict one"). That distinction is the
    fix: the previous mechanism loaded the active rows in Python, picked the
    single oldest with ``min()``, and revoked it — an action that is a
    conservation law once the count already sits at or above the cap (evict
    exactly one, mint exactly one nets to zero change), and that has no gap lock,
    so two concurrent logins can each see room for one more and both mint,
    netting +1. A single ``UPDATE ... WHERE id IN (SELECT ... OFFSET :limit)``
    has neither problem: any concurrent evaluation of it drives the active count
    to at most ``limit``, and it does the eviction and the accounting in one
    round trip with no Python-side row loading.

    ``ORDER BY created_at DESC, id DESC`` — the ``id DESC`` tiebreaker is
    load-bearing, not tidiness. ``created_at`` is ``server_default=func.now()``,
    i.e. transaction start time, so rows created in the same transaction or the
    same clock tick are exact ties; without a deterministic tiebreaker the row
    that was JUST inserted (in the same call, by
    :meth:`TokenService.create_refresh_token`) could land in the evicted set,
    logging a caller out with the very token they were just handed. ``id`` is a
    serial primary key, so higher id is strictly newer regardless of clock
    resolution.

    Args:
        db: Database session. The caller owns the transaction; this does not
            commit (matches :meth:`revoke_all_user_tokens_in_transaction`'s
            contract — the caller decides when the eviction becomes durable).
        user_id: Whose sessions to cap.
        limit: How many of the newest active sessions to keep. A caller wanting
            "make room for one more" passes ``max_concurrent_sessions - 1``.
        batch_limit: Caps how many rows a single call may revoke, bounding the
            per-call cost against a large pre-existing backlog. ``None`` means
            no cap (revoke everything beyond ``limit`` in one statement).

    Returns:
        The JTIs of the sessions this call revoked (empty if none were over the
        limit). A negative ``limit`` is clamped to zero before it reaches
        ``OFFSET`` (which raises on a negative value), so it means "keep none".
    """
    # One static query, always parameterized — never build SQL by interpolating a
    # clause string (that pattern trips S608 even when, as here, the interpolated
    # value only ever comes from a fixed pair of literals, and the fix that
    # actually addresses the finding is not having a second query shape at all).
    # ``LIMIT NULL`` is Postgres for "no limit", so passing ``batch_limit=None``
    # straight through as a bind parameter covers the unbounded case with the
    # exact same statement.
    query = text(
        """
        UPDATE refresh_token
           SET revoked_at = now()
         WHERE revoked_at IS NULL
           AND id IN (
               SELECT id
                 FROM refresh_token
                WHERE user_id = :user_id
                  AND revoked_at IS NULL
                  AND expires_at > now()
                ORDER BY created_at DESC, id DESC
               OFFSET :limit
                LIMIT :batch_limit
           )
        RETURNING jti
        """
    )

    result = db.execute(
        query, {"user_id": user_id, "limit": max(limit, 0), "batch_limit": batch_limit}
    )
    revoked_jtis = [row[0] for row in result.fetchall()]

    if revoked_jtis:
        logger.info(
            "Session ceiling enforcement revoked %d session(s) for user %s (limit=%d)",
            len(revoked_jtis),
            user_id,
            limit,
        )

    return revoked_jtis
