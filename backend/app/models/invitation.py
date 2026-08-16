"""Admin invitations and email-verification tokens (migration ``v375``).

Both models follow :mod:`app.models.password_reset`: only a SHA-256 hash of the
token is stored, ``expires_at`` bounds its life, and ``used_at`` makes it
single-use. A leaked database therefore yields no usable link.

``UserInvitation`` deliberately declares **no relationships**. It has two FKs to
``user`` (the inviting admin and the account the invite created), and two FKs to
the same table require an explicit ``foreign_keys=`` on *both* sides or mapper
configuration crashes at import time and the whole app fails to start — see
``app/models/CLAUDE.md``. Nothing needs the ORM traversal here.
"""

import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime

from sqlalchemy import CheckConstraint
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from app.db.base import Base
from app.utils.uuid7 import uuid7


class UserInvitation(Base):
    """An admin-issued, single-use invitation to create one account.

    The invitation carries the *target* ``role`` and ``auth_type`` so that a
    deployment whose IdP owns identity can pre-provision an LDAP/OIDC/PKI
    account: accepting the invite activates the row, and the external provider
    matches it at first login. An invitation for an external ``auth_type`` never
    stores a local password (``app/auth/utils.py:local_password_allowed`` is the
    single source of truth for whether an account may hold one).
    """

    __tablename__ = "user_invitation"

    # Both CHECKs mirror the ones on ``user`` (v377/v383) and are enforced by the
    # database today. As on ``user.auth_type``, the auth-type body is a **literal**
    # and must not be rebuilt from ``app.auth.constants.VALID_AUTH_TYPES``: the DB
    # CHECK is required to be a superset of that list, and deriving it here would
    # encode equality instead. See ``app/models/user.py`` for the full argument.
    __table_args__ = (
        CheckConstraint(
            "role IN ('user', 'admin', 'super_admin')",
            name="ck_user_invitation_role_valid",
        ),
        CheckConstraint(
            "auth_type IN ('local', 'ldap', 'oidc', 'pki', 'proxy', 'saml')",
            name="ck_user_invitation_auth_type_valid",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    #: The only identifier exposed through the API (hybrid-ID rule).
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: CHECK-constrained to VALID_ROLES in the DDL, like ``user.role``.
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="user")
    #: CHECK-constrained to VALID_AUTH_TYPES in the DDL, like ``user.auth_type``.
    auth_type: Mapped[str] = mapped_column(String(20), nullable=False, default="local")
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    #: Set when the invitation is accepted; the account it produced.
    created_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    def is_redeemable(self, now: datetime | None = None) -> bool:
        """Whether this invitation may still be accepted.

        Evaluated in Python rather than in the WHERE clause so that every
        rejection reason — unknown, used, revoked, expired — returns through the
        same code path with the same generic message. Distinguishing them tells a
        caller holding a guessed token which guesses were "close".
        """
        moment = now or datetime.now(UTC)
        expires_at = self.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return self.used_at is None and self.revoked_at is None and expires_at > moment


class EmailVerificationToken(Base):
    """Proof-of-control token for an account's email address.

    Distinct from ``ExternalIdentity.email_verified``, which records that an
    *IdP* asserted the address. This one records that this deployment sent mail
    to the address and someone holding it came back.
    """

    __tablename__ = "email_verification_token"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)


__all__ = ["EmailVerificationToken", "UserInvitation"]
