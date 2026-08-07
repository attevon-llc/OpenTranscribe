"""
RefreshToken model for JWT refresh token management.

Stores refresh token metadata for token revocation and session tracking.
Part of FedRAMP AC-12 compliance for token management.
"""

from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.user import User


class RefreshToken(Base):
    """
    Model for storing refresh token metadata.

    **This row is the session.** There is no second session record: concurrent-
    session limits, rotation, revocation, the fail-closed revocation fallback and
    (since ``v375``) idle/absolute timeouts all key off these rows. The Redis-
    backed ``SessionManager`` that used to duplicate the timeout half was deleted
    rather than wired up — two owners would enforce against different session
    sets the moment Redis and Postgres diverged. See
    ``plans/session-ownership-decision.md``.

    Stores the hash of the refresh token (not the token itself) along with
    expiration and revocation information. This allows for:
    - Token validation without storing the actual token
    - Token revocation by marking tokens as revoked
    - Session management by tracking all user tokens
    - Automatic cleanup of expired tokens

    Attributes:
        id: Primary key
        user_id: Foreign key to user table
        token_hash: SHA-256/SHA-512 hash of the refresh token (up to 128 chars hex)
        jti: JWT ID claim from the token for Redis blacklist lookup
        expires_at: Token expiration timestamp
        revoked_at: Timestamp when token was revoked (null if active)
        created_at: Token creation timestamp
        last_activity_at: When this session last presented a refresh token
        absolute_expires_at: Hard ceiling for the whole session, never extended
        oidc_id_token: Encrypted OIDC ID token, for RP-initiated logout
        user_agent: Optional user agent string for session identification
        ip_address: Optional IP address for session identification
    """

    __tablename__ = "refresh_token"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    jti: Mapped[str] = mapped_column(
        String(36), unique=True, nullable=False, index=True
    )  # UUID format
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    #: When this session last exchanged a refresh token. Stamped at issue, so a
    #: rotated row carries the rotation time. Compared against
    #: ``session_idle_timeout_minutes``.
    #:
    #: NULLABLE on purpose: rows that predate ``v375`` have no recorded activity
    #: and must not be invalidated by the upgrade — see :attr:`absolute_expires_at`.
    last_activity_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Hard ceiling on the whole session, set once when it is established and
    #: **carried forward unchanged through every rotation**. This is the only
    #: thing that caps a client which refreshes forever; ``expires_at`` moves
    #: with each rotation and therefore caps nothing.
    #:
    #: NULL means "no cap recorded" (a session established before ``v375``) and
    #: is treated as valid; the next rotation stamps a real ceiling on the
    #: successor row.
    absolute_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: The OIDC ID token this session was established with, encrypted at rest.
    #:
    #: RP-Initiated Logout 1.0 needs it as ``id_token_hint``, so it has to outlive the
    #: callback — but it carries the user's full identity claim set, so it lives
    #: **here and never in a cookie**. The obvious reference implementation puts it in
    #: a browser cookie by default and its own documentation calls that unsafe. On
    #: this row its lifetime is the session's: rotation, revocation and the
    #: concurrent-session cap already delete these rows, so nothing extra has to
    #: remember to clean it up. NULL for every non-OIDC session.
    oidc_id_token: Mapped[str | None] = mapped_column(Text, nullable=True)

    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)  # IPv6 max length

    # Relationship to user
    user: Mapped["User"] = relationship("User", back_populates="refresh_tokens")

    @property
    def is_revoked(self) -> bool:
        """Check if the token has been revoked."""
        return self.revoked_at is not None

    @property
    def is_expired(self) -> bool:
        """Check if the token has expired."""
        from datetime import datetime

        return bool(datetime.now(UTC) > self.expires_at)

    @property
    def is_valid(self) -> bool:
        """Check if the token is valid (not revoked and not expired)."""
        return not self.is_revoked and not self.is_expired
