"""Refresh-token lifecycle: creation, verification, session lifetime and rotation.

Mixed into :class:`~app.auth.token_service.TokenService`, which owns the revocation
store, token hashing, signature verification and the revocation plane these methods
call. ``RefreshTokenOps`` declares those members under ``TYPE_CHECKING`` so the type
checker verifies every call here against ``TokenService``'s real signatures; nothing
beyond the four methods exists at runtime.

Implements the FedRAMP AC-12 session controls that ride on refresh tokens:
- refresh tokens stored as SHA-512 hashes (FIPS 140-3 compliant)
- the concurrent-session ceiling enforced at every session mint (issue #632)
- the idle and absolute session caps checked at every refresh
- OAuth 2.1 rotation that carries the session's absolute ceiling forward
"""

import logging
import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING

from joserfc import jwt
from joserfc.errors import ExpiredTokenError
from joserfc.errors import JoseError
from joserfc.jwk import OctKey
from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from app.auth.audit import audit_logger
from app.auth.audit_events import AuditEventType
from app.auth.audit_events import AuditOutcome
from app.auth.revocation_error import RevocationError
from app.auth.session_ceiling import _session_lifetime_minutes
from app.auth.session_ceiling import enforce_session_ceiling
from app.core.auth_types import TOKEN_TYPE_REFRESH
from app.core.config import settings
from app.core.constants import SESSION_CAP_EVICTION_BATCH_LIMIT
from app.core.security import signing_algorithm
from app.models.refresh_token import RefreshToken
from app.utils.time_format import as_utc

logger = logging.getLogger(__name__)


class RefreshTokenReplayError(Exception):
    """The presented refresh token was already rotated or revoked by the time it was claimed.

    Raised by :meth:`RefreshTokenOps.rotate_refresh_token` when its conditional claim
    matched no row: another request spent the same token first. The caller answers 401
    and runs :meth:`RefreshTokenOps.revoke_reused_token_family` (RFC 9700 §4.14.2).
    """


class RefreshTokenOps:
    """Refresh-token operations mixed into :class:`~app.auth.token_service.TokenService`."""

    if TYPE_CHECKING:

        @staticmethod
        def _hash_token(token: str) -> str: ...

        def verify_token_with_fallback(self, token: str) -> dict: ...

        def is_token_revoked(
            self,
            jti: str,
            db: Session | None = None,
            user_uuid: str | None = None,
            issued_at: int | None = None,
        ) -> bool: ...

        def revoke_token(
            self, db: Session, jti: str, expires_at: datetime | None = None
        ) -> None: ...

        def _publish_revocation(
            self, jti: str, expires_at: datetime | None, owner_id: int | None, *, durable: bool
        ) -> None: ...

    def create_refresh_token(
        self,
        db: Session,
        user_id: int,
        user_uuid: str,
        role: str,
        user_agent: str | None = None,
        ip_address: str | None = None,
        absolute_expires_at: datetime | None = None,
        *,
        enforce_concurrent_limit: bool = True,
    ) -> tuple[str, RefreshToken]:
        """
        Create a new refresh token for a user.

        Creates a JWT refresh token with:
        - User UUID in 'sub' claim
        - User role in 'role' claim
        - Unique JTI for revocation tracking
        - Token type claim set to 'refresh'

        Stores token hash in database for validation and revocation.

        **This is the universal backstop for the concurrent-session ceiling
        (issue #632).** Every refresh_token row is created here, whatever the
        auth method — local/LDAP login, OIDC, SAML, PKI, the proxy method, MFA
        enrollment, and `account_security_service.reissue_current_session` — so
        this is the one place that has to enforce the cap for all of them to be
        covered. Before this, only `login.py`'s own duplicated check enforced it,
        which meant every other minting path was completely uncapped.

        Args:
            db: Database session
            user_id: User's database ID
            user_uuid: User's UUID string
            role: User's role (for inclusion in token)
            user_agent: Optional user agent for session tracking
            ip_address: Optional IP address for session tracking
            absolute_expires_at: The session's existing hard ceiling, carried
                forward by :meth:`rotate_refresh_token`. ``None`` establishes a
                NEW session and stamps a fresh ceiling — passing the predecessor's
                value is what stops rotation from renewing a session forever.
            enforce_concurrent_limit: Whether this call should enforce
                ``max_concurrent_sessions`` after inserting the new row.
                :meth:`rotate_refresh_token` passes ``False`` explicitly — a
                rotation is not a new session, and capping there would let one
                busy client evict OTHER devices' sessions purely by refreshing
                its own. Every other caller keeps the default ``True``.

        Returns:
            Tuple of (token_string, RefreshToken model instance)
        """
        # Generate unique JTI
        jti = str(uuid.uuid4())

        # Calculate expiration
        now = datetime.now(UTC)
        expires_delta = timedelta(days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS)
        expires_at = now + expires_delta

        # The session ceiling, and — riding along on the SAME get_auth_settings(db)
        # resolution — the concurrent-session cap this call will enforce below.
        # Only computed when no absolute_expires_at was carried in: that is exactly
        # the "this is a new session, not a rotation" case, which is also the only
        # case enforcement ever applies (rotate_refresh_token always passes
        # enforce_concurrent_limit=False alongside its carried-forward ceiling).
        max_concurrent_sessions = 0
        if absolute_expires_at is None:
            _idle_minutes, absolute_minutes, max_concurrent_sessions = _session_lifetime_minutes(db)
            absolute_expires_at = now + timedelta(minutes=absolute_minutes)

        # Create token payload
        token_data = {
            "sub": user_uuid,
            "role": role,
            "jti": jti,
            "iat": now,
            "exp": expires_at,
            "type": "refresh",
        }

        # One owner for "which algorithm signs this", shared with every other issuer.
        #
        # This line read `JWT_ALGORITHM_V3 if FIPS_VERSION == "140-3" else "HS256"`,
        # gated on FIPS_VERSION — which defaults to "140-3". So EVERY deployment,
        # FIPS or not, signed its refresh tokens with HS512 while its access tokens
        # were HS256. It was invisible because the only refresh verifier tried both
        # algorithms; the visible symptom was that `POST /auth/logout` presented with
        # a refresh token (which sessions.py explicitly supports — "logging out with a
        # refresh token is still a logout") decoded with `[settings.JWT_ALGORITHM]`
        # and therefore never revoked anything.
        algorithm = signing_algorithm(TOKEN_TYPE_REFRESH)
        key = OctKey.import_key(settings.JWT_SECRET_KEY)
        token = jwt.encode({"alg": algorithm}, token_data, key, algorithms=[algorithm])

        # Hash token for storage
        token_hash = self._hash_token(token)

        # Create database record
        refresh_token = RefreshToken(
            user_id=user_id,
            token_hash=token_hash,
            jti=jti,
            expires_at=expires_at,
            user_agent=user_agent,
            ip_address=ip_address,
            last_activity_at=now,
            absolute_expires_at=absolute_expires_at,
        )

        db.add(refresh_token)
        # Flush (not commit) first: the new row needs an id and to be visible to the
        # ceiling UPDATE below, but the insert and the eviction must land in ONE
        # transaction — flush-then-enforce-then-commit is what closes the window a
        # separate commit would leave between "session created" and "cap enforced".
        db.flush()

        # Deliberate: this enforces a ceiling regardless of
        # `concurrent_session_policy` ("terminate_oldest" vs "reject"). That
        # policy is `login.py`'s alone to interpret — it decides whether an
        # interactive login at the cap gets a 429 or an eviction. Every OTHER
        # minting path reaching this backstop (OIDC, SAML, PKI, proxy, MFA
        # enrollment, `reissue_current_session`) enforced NOTHING before #632, so
        # for them the choice is "ceiling enforcement" vs "no enforcement at
        # all", not "ceiling enforcement" vs "reject" — and leaving them
        # uncapped under a deployment's "reject" policy is worse than capping
        # them. `login.py` never reaches this branch with a full session in the
        # reject case, since it raises 429 before calling `_generate_login_tokens`.
        if enforce_concurrent_limit and max_concurrent_sessions > 0:
            enforce_session_ceiling(
                db,
                user_id,
                max_concurrent_sessions,
                batch_limit=SESSION_CAP_EVICTION_BATCH_LIMIT,
            )

        db.commit()
        db.refresh(refresh_token)

        logger.info(
            f"Created refresh token for user {user_id} "
            f"(jti={jti[:8]}..., expires={expires_at.isoformat()})"
        )

        # Single choke point: every session (refresh_token row) is created here,
        # whatever the auth method or whether this is a fresh login or a rotation
        # — so this is the one place that needs to emit it, not every call site.
        audit_logger.log(
            event_type=AuditEventType.AUTH_SESSION_CREATED,
            outcome=AuditOutcome.SUCCESS,
            user_id=user_id,
            source_ip=ip_address,
            user_agent=user_agent,
            details={"jti": jti},
        )

        return token, refresh_token

    def verify_refresh_token(
        self, db: Session, token: str
    ) -> tuple[dict | None, RefreshToken | None]:
        """
        Verify a refresh token and return its payload.

        Validation steps:
        1. Decode and verify JWT signature
        2. Check token type is 'refresh'
        3. Check token hash exists in database
        4. Check token is not revoked in database
        5. Check token is not on Redis blacklist
        6. Check token is not expired
        7. Check the SESSION is still within its idle and absolute caps

        Step 7 is what makes ``session_idle_timeout_minutes`` and
        ``session_absolute_timeout_minutes`` real. It fires once per access-token
        lifetime rather than per request, deliberately: per-request activity
        tracking would be refreshed continuously by polling endpoints (progress,
        notifications, task status) and WebSocket keepalives, so idle timeout
        would appear to work and never actually fire — worse than not having it,
        because it reads as a satisfied control.

        Args:
            db: Database session
            token: The refresh token string to verify

        Returns:
            Tuple of (payload_dict, RefreshToken model) if valid, (None, None) if invalid
        """
        try:
            # Decode token with algorithm fallback for FIPS 140-3 migration
            payload = self.verify_token_with_fallback(token)

            # Verify token type
            if payload.get("type") != "refresh":
                logger.warning("Token verification failed: not a refresh token")
                return None, None

            jti = payload.get("jti")
            if not jti:
                logger.warning("Token verification failed: missing JTI")
                return None, None

            # Check Redis blacklist first (fast path)
            if self.is_token_revoked(jti, db=db):
                logger.warning(f"Token verification failed: JTI {jti[:8]}... is revoked")
                return None, None

            # Find token in database
            token_hash = self._hash_token(token)
            refresh_token = (
                db.query(RefreshToken).filter(RefreshToken.token_hash == token_hash).first()
            )

            if not refresh_token:
                logger.warning("Token verification failed: token not found in database")
                return None, None

            # Check database revocation status
            if refresh_token.is_revoked:
                # is_revoked is True iff revoked_at is not None (see model property)
                assert refresh_token.revoked_at is not None
                logger.warning(
                    f"Token verification failed: token revoked at "
                    f"{refresh_token.revoked_at.isoformat()}"
                )
                return None, None

            # Check expiration (should be caught by JWT decode, but double-check)
            if refresh_token.is_expired:
                logger.warning("Token verification failed: token expired")
                audit_logger.log(
                    event_type=AuditEventType.AUTH_SESSION_EXPIRED,
                    outcome=AuditOutcome.FAILURE,
                    user_id=refresh_token.user_id,
                    details={"jti": jti},
                )
                return None, None

            if not self._session_within_lifetime(db, refresh_token):
                return None, None

            logger.debug(f"Refresh token verified successfully (jti={jti[:8]}...)")
            return payload, refresh_token

        except ExpiredTokenError:
            logger.warning("Token verification failed: token expired")
            return None, None
        except JoseError as e:
            logger.warning(f"Token verification failed: JWT error - {e}")
            return None, None
        except Exception as e:
            logger.error(f"Token verification error: {e}")
            return None, None

    @staticmethod
    def _session_within_lifetime(db: Session, refresh_token: RefreshToken) -> bool:
        """Whether *refresh_token*'s session is still inside its idle and absolute caps.

        Both columns are nullable and a NULL is treated as "no cap recorded", not
        as an expiry. Sessions that predate ``v377`` carry NULL in both, and
        invalidating them on upgrade would sign every user out for a second time
        in the same release (the token-type change already does it once). The next
        rotation stamps real values on the successor row.

        Args:
            db: Database session, used to resolve the DB-backed timeouts.
            refresh_token: The stored session row being verified.

        Returns:
            True when the session may continue.
        """
        now = datetime.now(UTC)

        absolute_expires_at = as_utc(refresh_token.absolute_expires_at)
        if absolute_expires_at is not None and now > absolute_expires_at:
            logger.info(
                "Session refused: absolute timeout reached at %s (jti=%s...)",
                absolute_expires_at.isoformat(),
                str(refresh_token.jti)[:8],
            )
            return False

        idle_minutes, _absolute_minutes, _max_concurrent_sessions = _session_lifetime_minutes(db)
        last_activity_at = as_utc(refresh_token.last_activity_at)
        # 0 disables the idle cap. The admin UI's schema bounds it at 1, but
        # SESSION_IDLE_TIMEOUT_MINUTES is a plain .env integer with no bound, and
        # `now - last_activity > 0` would refuse every session on sight.
        idle_exceeded = (
            idle_minutes > 0
            and last_activity_at is not None
            and now - last_activity_at > timedelta(minutes=idle_minutes)
        )
        if idle_exceeded:
            logger.info(
                "Session refused: idle for more than %s minutes (last activity %s, jti=%s...)",
                idle_minutes,
                last_activity_at.isoformat() if last_activity_at else "unknown",
                str(refresh_token.jti)[:8],
            )
            return False

        return True

    def rotate_refresh_token(
        self,
        db: Session,
        old_token: str,
        old_token_record: RefreshToken,
        user_id: int,
        user_uuid: str,
        role: str,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> tuple[str, RefreshToken]:
        """
        Rotate a refresh token by revoking the old one and creating a new one.

        This implements OAuth 2.1 refresh token rotation best practice:
        - Claims (revokes) the old refresh token with one conditional UPDATE, so a
          token can be spent exactly once even under concurrent requests
        - Creates a new refresh token with fresh expiration, in the same transaction
        - Limits the impact of stolen refresh tokens

        The successor row inherits ``absolute_expires_at`` **unchanged**, so the
        session's ceiling is fixed by the login that established it. Recomputing
        it here would mean a client that refreshes before every expiry never has
        to re-authenticate, which is exactly the gap this column closes.
        ``last_activity_at``, by contrast, is stamped fresh on the new row — that
        is what "activity" means for the idle cap.

        Args:
            db: Database session
            old_token: The old refresh token string
            old_token_record: The RefreshToken model instance to revoke
            user_id: User's database ID
            user_uuid: User's UUID string
            role: User's role (for inclusion in token)
            user_agent: Optional user agent for session tracking
            ip_address: Optional IP address for session tracking

        Returns:
            Tuple of (new_token_string, new_RefreshToken model instance)

        Raises:
            RefreshTokenReplayError: The token was already spent when claimed.
            RevocationError: The rotation could not be recorded; nothing changed.
        """
        # Claim the old token with ONE conditional UPDATE (PostgreSQL docs, "Read Committed
        # Isolation Level"): a concurrent refresh presenting the same token blocks on the
        # row lock, re-evaluates `revoked_at IS NULL` against the committed row once this
        # transaction ends, and matches nothing. A read-then-revoke here minted two
        # successors from one token. The claim and the successor commit together, so a
        # failure before the commit leaves the old token valid and the client can retry.
        old_jti = str(old_token_record.jti)
        old_expires = as_utc(old_token_record.expires_at)
        carried_ceiling = as_utc(old_token_record.absolute_expires_at)
        try:
            claimed = db.execute(
                update(RefreshToken)
                .where(RefreshToken.jti == old_jti, RefreshToken.revoked_at.is_(None))
                .values(revoked_at=func.now())
                .returning(RefreshToken.id)
            ).first()
        except SQLAlchemyError as exc:
            db.rollback()
            raise RevocationError("rotation could not be recorded") from exc
        if claimed is None:
            db.rollback()
            raise RefreshTokenReplayError(old_jti)

        # A legacy row (pre-v377) carries NULL here; passing it through means the
        # successor gets a freshly-stamped ceiling rather than the session being
        # refused, which is how NULL is grandfathered.
        #
        # enforce_concurrent_limit=False: a rotation is not a new session, it is the
        # SAME session continuing. Enforcing the cap here would let one busy client
        # evict OTHER devices' sessions purely by refreshing its own token — the cap
        # is a login-time and backstop-time control, not a per-refresh one (issue #632).
        try:
            new_token, new_token_record = self.create_refresh_token(
                db=db,
                user_id=user_id,
                user_uuid=user_uuid,
                role=role,
                user_agent=user_agent,
                ip_address=ip_address,
                absolute_expires_at=carried_ceiling,
                enforce_concurrent_limit=False,
            )
        except SQLAlchemyError as exc:
            db.rollback()
            raise RevocationError("rotation could not be recorded") from exc

        self._publish_revocation(old_jti, old_expires, user_id, durable=True)
        logger.info(f"Rotated out old refresh token for user {user_id} (old_jti={old_jti[:8]}...)")

        return new_token, new_token_record

    def revoke_reused_token_family(self, db: Session, token: str) -> int:
        """Revoke the live remainder of a session whose spent refresh token came back.

        RFC 9700 §4.14.2: when a refresh token that was already rotated is presented
        again, either the legitimate client or an attacker holds a copy, and the server
        cannot tell which, so the whole token family is revoked. A family is every row
        of one session: rotation carries ``absolute_expires_at`` forward unchanged, so
        the rows of one chain share it exactly and no two logins do. A legacy row
        (pre-v377, NULL ceiling) cannot be linked to its successors, so for it every
        live session of the user is revoked instead.

        Only a token whose signature verifies and whose row exists and is revoked
        counts; a garbage or unknown token changes nothing. A revoked token whose
        session has no live rows left (a plain logout, an eviction) revokes nothing
        and is not reported as reuse.

        Args:
            db: Database session; committed here.
            token: The refresh token string that was refused.

        Returns:
            How many live sessions were revoked (0 when this was not a reuse).
        """
        try:
            payload = self.verify_token_with_fallback(token)
        except JoseError:
            return 0
        if payload.get("type") != "refresh":
            return 0
        spent = (
            db.query(RefreshToken)
            .filter(RefreshToken.token_hash == self._hash_token(token))
            .first()
        )
        if spent is None or spent.revoked_at is None:
            return 0
        user_id = int(spent.user_id)
        spent_jti = str(spent.jti)
        conditions = [RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)]
        if spent.absolute_expires_at is not None:
            conditions.append(RefreshToken.absolute_expires_at == spent.absolute_expires_at)
        try:
            revoked = db.execute(
                update(RefreshToken)
                .where(*conditions)
                .values(revoked_at=func.now())
                .returning(RefreshToken.jti, RefreshToken.expires_at)
            ).all()
            db.commit()
        except SQLAlchemyError as exc:
            db.rollback()
            raise RevocationError("reuse revocation could not be recorded") from exc
        for jti, expires_at in revoked:
            self._publish_revocation(str(jti), as_utc(expires_at), user_id, durable=True)
        if revoked:
            logger.warning(
                "Refresh token reuse detected for user %s; revoked %d session(s)",
                user_id,
                len(revoked),
            )
            audit_logger.log(
                event_type=AuditEventType.AUTH_TOKEN_REUSE,
                outcome=AuditOutcome.FAILURE,
                user_id=user_id,
                details={"jti": spent_jti, "sessions_revoked": len(revoked)},
            )
        return len(revoked)
