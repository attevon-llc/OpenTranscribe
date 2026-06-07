"""Organization (tenant) models — cloud-edition seam.

Mirrors the external IdP's organization + membership state so resources can be
org-scoped and quotas enforced per tenant. In the community/self-hosted edition
these tables simply stay empty (everything is personal, ``organization_id`` is
NULL everywhere).

Billing columns (``subscription_*``, ``stripe_*``, ``hours_*``, ``seats_limit``)
are READ-ONLY to core: the private cloud layer writes them from its billing
webhooks; core only displays them.
"""

import uuid as uuid_pkg
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import Numeric
from sqlalchemy import SmallInteger
from sqlalchemy import String
from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base
from app.utils.uuid7 import uuid7

if TYPE_CHECKING:
    from app.models.user import User


class Organization(Base):
    __tablename__ = "organization"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    clerk_org_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Billing state — written by the private cloud layer, read-only to core.
    subscription_tier: Mapped[str] = mapped_column(String(20), nullable=False, default="community")
    subscription_status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    seats_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hours_limit_per_month: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 3), nullable=True
    )  # NULL = unlimited
    hours_used_this_month: Mapped[Decimal] = mapped_column(
        Numeric(10, 3), nullable=False, default=0
    )
    billing_cycle_anchor_day: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    memberships: Mapped[list["OrganizationMembership"]] = relationship(
        "OrganizationMembership", back_populates="organization", cascade="all, delete-orphan"
    )


class OrganizationMembership(Base):
    """Mirror of external-IdP org membership (refreshed by webhooks + on login).

    Authorization checks run against THIS table, not just the token, so a
    removed member loses access even before their short-lived token expires.
    """

    __tablename__ = "organization_membership"
    __table_args__ = (UniqueConstraint("organization_id", "user_id", name="uq_org_membership"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organization.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(
        String(20), nullable=False, default="org:member"
    )  # org:admin | org:member
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    organization: Mapped["Organization"] = relationship(
        "Organization", back_populates="memberships"
    )
    user: Mapped["User"] = relationship("User", back_populates="org_memberships")
