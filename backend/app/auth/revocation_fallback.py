"""Database answers for token revocation while Redis cannot be consulted.

Redis is a cache of the durable ``refresh_token.revoked_at``; when it is
unreachable these functions answer from the system of record.
"""

import logging
from datetime import UTC
from datetime import datetime

from sqlalchemy.orm import Session

from app.auth.degradation import record_degradation
from app.models.refresh_token import RefreshToken
from app.models.user import User

logger = logging.getLogger(__name__)


def is_revoked_from_database(
    db: Session | None,
    jti: str,
    user_uuid: str | None,
) -> bool:
    """Answer "is this token revoked?" from the database instead of Redis.

    Cache-aside: on cache failure, consult the system of record. Returning
    the per-process store's answer instead would report "not revoked" for
    everything, because that store is empty in a fresh process and is never
    shared between replicas.

    Two cases, because access tokens have no ``refresh_token`` row:

    1. **The JTI is a refresh token.** ``revoked_at`` is authoritative.
    2. **The JTI is an access token.** Nothing durable records it, so fall
       back to the user: if every one of their refresh tokens is revoked,
       their sessions were terminated (logout-all, password reset, admin
       action) and this access token goes with them. That covers the reason
       revocation is actually used. Access tokens are short-lived, so an
       unrevoked-user access token is bounded by its own expiry anyway.

    Without a session, or if the query itself fails, this **denies**. A
    wrongly-denied request costs a re-authentication; a wrongly-allowed one
    honours a token someone explicitly revoked.
    """
    if db is None:
        record_degradation("token_revocation", "deny")
        logger.critical(
            "Cannot verify revocation for jti=%s... — Redis is down and no database "
            "session was supplied. Denying (fail closed).",
            jti[:8],
        )
        return True

    try:
        row = db.query(RefreshToken).filter(RefreshToken.jti == jti).first()
        if row is not None:
            record_degradation("token_revocation", "database")
            return row.revoked_at is not None

        if user_uuid is None:
            record_degradation("token_revocation", "deny")
            return True

        # Access token: no durable row of its own, so infer from the user's
        # sessions — but only on POSITIVE evidence of revocation.
        #
        # "This user has no refresh tokens" is NOT such evidence. Some auth
        # paths never mint one, so treating absence as revocation would lock
        # those users out every time Redis blinked. Deny only when the user
        # demonstrably had sessions and every one of them was revoked, which
        # is what logout-all / password reset / admin termination produce.
        # Otherwise allow, bounded by the access token's own short expiry.
        has_any = (
            db.query(RefreshToken)
            .join(User, User.id == RefreshToken.user_id)
            .filter(User.uuid == user_uuid)
            .first()
        )
        record_degradation("token_revocation", "database")
        if has_any is None:
            return False

        live = (
            db.query(RefreshToken)
            .join(User, User.id == RefreshToken.user_id)
            .filter(
                User.uuid == user_uuid,
                RefreshToken.revoked_at.is_(None),
                RefreshToken.expires_at > datetime.now(UTC),
            )
            .first()
        )
        return live is None
    except Exception:
        record_degradation("token_revocation", "deny")
        logger.critical(
            "Database revocation fallback failed for jti=%s...; denying (fail closed).",
            jti[:8],
            exc_info=True,
        )
        return True
