"""Authentication configuration database models for super admin UI."""

import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.db.base import Base
from app.utils.uuid7 import uuid7

if TYPE_CHECKING:
    from app.models.user import User


class AuthConfig(Base):
    """Stores authentication configuration settings.

    This table holds all configurable authentication settings for the application,
    including LDAP, Keycloak, PKI, MFA, password policy, and session configurations.
    Sensitive values (like passwords and secrets) are encrypted at rest.
    """

    __tablename__ = "auth_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7
    )
    config_key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    config_value: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # Encrypted for sensitive fields
    is_sensitive: Mapped[bool | None] = mapped_column(Boolean, default=False)
    category: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True
    )  # ldap, keycloak, pki, local, mfa, password_policy, session
    data_type: Mapped[str | None] = mapped_column(
        String(20), default="string"
    )  # string, int, bool, json
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    requires_restart: Mapped[bool | None] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    created_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("user.id"), nullable=True)
    updated_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("user.id"), nullable=True)

    # Relationships
    creator: Mapped["User | None"] = relationship("User", foreign_keys=[created_by])
    updater: Mapped["User | None"] = relationship("User", foreign_keys=[updated_by])


class AuthConfigAudit(Base):
    """Audit log for authentication configuration changes.

    Records all changes to authentication configuration settings for
    security compliance and troubleshooting purposes. Sensitive values
    are masked in the audit log.
    """

    __tablename__ = "auth_config_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7
    )
    config_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    old_value: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # Masked for sensitive fields
    new_value: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # Masked for sensitive fields
    changed_by: Mapped[int] = mapped_column(Integer, ForeignKey("user.id"), nullable=False)
    change_type: Mapped[str] = mapped_column(String(20), nullable=False)  # create, update, delete
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    # Relationships
    user: Mapped["User"] = relationship("User", foreign_keys=[changed_by])
