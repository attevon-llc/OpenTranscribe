"""Support-access grants: how platform staff reach a tenant's content (issue #1122).

In a multi-tenant deployment ``role in {admin, super_admin}`` grants no implicit access to
tenant content. Staff reach it only through a grant: requested with a reason and approved by
the tenant (``approved``), or opened by a ``super_admin`` in an emergency with a reason and
a ticket (``break_glass``). Every request carried under a grant is recorded in
``support_access_use``.

Two decisions shape the schema:

* **``SET NULL`` on every foreign key, never ``CASCADE``.** A grant is the evidence that
  support touched a tenant. Cascading from the organization or the user would delete that
  evidence exactly when the tenant or the account is erased. A grant whose target FK has
  been nulled is permanently unusable (``support_grant_invalid``), and its row remains.
* **``support_access_use`` carries ids, never content.** ``organization_id`` and
  ``owner_user_id`` are snapshot stamps with deliberately NO foreign key (the
  ``erasure_ledger`` pattern), so they survive the erasure of the rows they name. The row
  holds no filename, no title, no name: a route template and a resource uuid.

``status`` is computed (``services/support_access_service.compute_status``), never stored,
so it cannot disagree with the timestamps that define it.
"""

import uuid as uuid_pkg
from datetime import datetime

from sqlalchemy import BigInteger
from sqlalchemy import CheckConstraint
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.sql import func

from app.db.base import Base
from app.utils.uuid7 import uuid7

TARGET_KINDS = ("organization", "personal")
ACCESS_LEVELS = ("read", "write")
GRANT_MODES = ("approved", "break_glass")
DECISIONS = ("approved", "denied")
USE_NEEDS = ("read", "write")


def _sql_in(column: str, values: tuple[str, ...]) -> str:
    joined = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({joined})"


class SupportAccessGrant(Base):
    """One request, approval or break-glass opening of platform access to a tenant."""

    __tablename__ = "support_access_grant"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: The only identifier on the wire.
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    target_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    organization_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("organization.id", ondelete="SET NULL"), nullable=True
    )
    subject_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    #: The staff member the grant is bound to; a leaked uuid is useless to anyone else.
    grantee_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    access_level: Mapped[str] = mapped_column(String(10), nullable=False)
    grant_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    ticket_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    requested_duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    decided_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision: Mapped[str | None] = mapped_column(String(10), nullable=True)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            _sql_in("target_kind", TARGET_KINDS), name="ck_support_access_grant_target_kind"
        ),
        CheckConstraint(
            "NOT (target_kind = 'organization' AND subject_user_id IS NOT NULL)",
            name="ck_support_access_grant_org_target_no_subject",
        ),
        CheckConstraint(
            "NOT (target_kind = 'personal' AND organization_id IS NOT NULL)",
            name="ck_support_access_grant_personal_target_no_org",
        ),
        CheckConstraint(
            _sql_in("access_level", ACCESS_LEVELS), name="ck_support_access_grant_access_level"
        ),
        CheckConstraint(
            _sql_in("grant_mode", GRANT_MODES), name="ck_support_access_grant_grant_mode"
        ),
        CheckConstraint(
            f"decision IS NULL OR {_sql_in('decision', DECISIONS)}",
            name="ck_support_access_grant_decision",
        ),
        CheckConstraint(
            "requested_duration_minutes BETWEEN 15 AND 480",
            name="ck_support_access_grant_duration",
        ),
        CheckConstraint(
            "grant_mode <> 'break_glass' OR "
            "(ticket_ref IS NOT NULL AND requested_duration_minutes <= 240)",
            name="ck_support_access_grant_break_glass",
        ),
        CheckConstraint(
            "char_length(reason) BETWEEN 10 AND 2000", name="ck_support_access_grant_reason_length"
        ),
        CheckConstraint(
            "expires_at IS NULL OR expires_at > starts_at", name="ck_support_access_grant_window"
        ),
        Index("ix_support_access_grant_grantee_expires", "grantee_user_id", "expires_at"),
        Index("ix_support_access_grant_org_requested", "organization_id", "requested_at"),
        Index("ix_support_access_grant_subject_requested", "subject_user_id", "requested_at"),
    )


class SupportAccessUse(Base):
    """Append-only record that a request was served under a grant. Ids only, no content."""

    __tablename__ = "support_access_use"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    #: RESTRICT: a grant that has uses cannot be deleted out from under them.
    grant_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("support_access_grant.id", ondelete="RESTRICT"), nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    #: The route TEMPLATE (``/api/files/{file_uuid}``), never the raw path.
    route: Mapped[str] = mapped_column(String(255), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    resource_uuid: Mapped[uuid_pkg.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    need: Mapped[str | None] = mapped_column(String(5), nullable=True)
    #: Snapshot stamps with NO foreign key, so they outlive the erasure of what they name.
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    owner_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint(
            f"need IS NULL OR {_sql_in('need', USE_NEEDS)}", name="ck_support_access_use_need"
        ),
        Index("ix_support_access_use_grant_occurred", "grant_id", "occurred_at"),
    )
