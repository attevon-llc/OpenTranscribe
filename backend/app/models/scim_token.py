"""Bearer credential for the SCIM 2.0 provisioning surface (``v380``).

One row per identity provider that provisions into this deployment. The token
itself is shown **once**, at creation, and only its SHA-256 digest is stored — the
same shape ``UserInvitation`` and ``EmailVerificationToken`` already use, and for the
same reason: a provisioning credential that can update user records and disable
accounts must not be readable out of a database dump.

Why not reuse the session/refresh-token machinery: a SCIM token belongs to an
integration rather than a person. It has no role, no user to revoke sessions for, no
rotation-on-use, and it must survive the departure of whichever administrator issued
it (``created_by`` is ``ON DELETE SET NULL`` for exactly that reason).
"""

import uuid as uuid_pkg
from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.sql import func

from app.db.base import Base
from app.utils.uuid7 import uuid7


class SCIMToken(Base):
    """A hashed, revocable bearer token for ``/scim/v2/*``."""

    __tablename__ = "scim_token"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    #: Operator-facing label ("Okta production"), so a revocation decision can be
    #: made without knowing the secret.
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    #: SHA-256 hex digest of the presented token. UNIQUE, and the only lookup key —
    #: verification is a single indexed equality, never a scan-and-compare.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    #: The super_admin who issued it. NULL once that account is deleted; the token
    #: keeps working, because provisioning must not break when an admin leaves.
    created_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    #: Optional expiry. NULL means "until revoked" — SCIM connectors are configured
    #: once and a surprise expiry is a silent provisioning outage, so unlike the
    #: user-facing API tokens this one does not force a lifetime.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Stamped on every successful verification, so an unused integration is visible.
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Set once; a revoked token is never re-enabled. Kept rather than deleted so the
    #: audit trail still resolves the name behind past provisioning events.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
