"""An acceptance of one exact version of a legal document (migrations v441, v450, v451)."""

import uuid as uuid_pkg
from datetime import datetime

from sqlalchemy import CHAR
from sqlalchemy import CheckConstraint
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.sql import func

from app.db.base import Base
from app.utils.uuid7 import uuid7


class LegalAcceptance(Base):
    """Which user (or, for ``scope='organization'``, which organization) accepted which text.

    ``sha256`` is the hash of the exact bytes served at acceptance time; a new version or
    a changed text therefore needs a new row. ``scope='user'`` with ``organization_id`` set is
    a personal acceptance made while acting in that organization; ``scope='organization'`` is
    an org admin accepting on the organization's behalf.

    Since v451 a row outlives the account (``ON DELETE SET NULL``). The database trigger
    ``trg_legal_acceptance_guard`` then blanks ``ip_address`` and ``user_agent``, sets
    ``subject_erased_at`` and ``retain_until``, and keeps ``subject_user_uuid``: the row is
    pseudonymised personal data, not anonymous. The same trigger makes the table append-only;
    only ``tasks/legal_retention.py`` may delete a row, after ``retain_until``.
    """

    __tablename__ = "legal_acceptance"
    __table_args__ = (
        Index(
            "uq_legal_acceptance_user_doc_sha",
            "user_id",
            "document_key",
            "sha256",
            unique=True,
            postgresql_where=text("scope = 'user'"),
        ),
        Index(
            "uq_legal_acceptance_org_doc_sha",
            "organization_uuid",
            "document_key",
            "sha256",
            unique=True,
            postgresql_where=text("scope = 'organization'"),
        ),
        CheckConstraint(
            "(user_id IS NOT NULL AND subject_erased_at IS NULL)"
            " OR (user_id IS NULL AND subject_erased_at IS NOT NULL AND retain_until IS NOT NULL"
            " AND ip_address IS NULL AND user_agent IS NULL)",
            name="ck_legal_acceptance_subject",
        ),
        Index(
            "ix_legal_acceptance_retain_until",
            "retain_until",
            postgresql_where=text("retain_until IS NOT NULL"),
        ),
        CheckConstraint(
            "(scope = 'user') OR (scope = 'organization' AND organization_uuid IS NOT NULL)",
            name="ck_legal_acceptance_scope",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="SET NULL"), nullable=True, index=True
    )
    document_key: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    locale: Mapped[str | None] = mapped_column(String(35), nullable=True)
    scope: Mapped[str] = mapped_column(
        String(16), nullable=False, default="user", server_default="user"
    )
    organization_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("organization.id", ondelete="SET NULL"), nullable=True, index=True
    )
    organization_uuid: Mapped[uuid_pkg.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    #: Set by a BEFORE INSERT trigger from the account; kept after the account is deleted.
    subject_user_uuid: Mapped[uuid_pkg.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    subject_erased_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retain_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
