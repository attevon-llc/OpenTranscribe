"""SQLAlchemy models for watch-source email notifications.

``EmailNotificationConfig`` holds a reusable, admin-managed email provider
configuration (SMTP / Microsoft 365 Graph / Exchange on-prem) with secrets
AES-256-GCM encrypted. ``WatchSourceEmail`` is the junction linking a watch
source to one or more email configs, with per-link recipient overrides and
success/error toggles.
"""

import uuid as uuid_pkg

from sqlalchemy import Boolean
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base


class EmailNotificationConfig(Base):
    """A reusable email provider configuration for notifications."""

    __tablename__ = "email_notification_config"

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid_pkg.uuid4, index=True
    )
    name = Column(String(200), nullable=False)
    provider = Column(String(20), nullable=False)  # smtp | m365 | exchange

    # ----- SMTP -----
    smtp_host = Column(String(255), nullable=True)
    smtp_port = Column(Integer, nullable=True)
    smtp_use_tls = Column(Boolean, default=True, nullable=False)
    smtp_username = Column(String(255), nullable=True)
    encrypted_smtp_password = Column(Text, nullable=True)  # AES-256-GCM encrypted

    # ----- Microsoft 365 (Graph OAuth2) -----
    m365_tenant_id = Column(String(255), nullable=True)
    m365_client_id = Column(String(255), nullable=True)
    encrypted_m365_client_secret = Column(Text, nullable=True)  # AES-256-GCM encrypted

    # ----- Exchange on-prem -----
    exchange_server = Column(String(255), nullable=True)
    exchange_domain = Column(String(255), nullable=True)
    exchange_username = Column(String(255), nullable=True)
    encrypted_exchange_password = Column(Text, nullable=True)  # AES-256-GCM encrypted

    # ----- common -----
    from_address = Column(String(255), nullable=True)
    default_recipients = Column(Text, nullable=True)  # CSV
    is_enabled = Column(Boolean, default=True, nullable=False)
    last_tested_at = Column(DateTime(timezone=True), nullable=True)
    test_status = Column(String(20), nullable=True)  # success | failed | untested
    test_message = Column(Text, nullable=True)

    created_by = Column(Integer, ForeignKey("user.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    links = relationship(
        "WatchSourceEmail", back_populates="email_config", cascade="all, delete-orphan"
    )

    @property
    def has_smtp_password(self) -> bool:
        return bool(self.encrypted_smtp_password)

    @property
    def has_m365_secret(self) -> bool:
        return bool(self.encrypted_m365_client_secret)

    @property
    def has_exchange_password(self) -> bool:
        return bool(self.encrypted_exchange_password)

    def __repr__(self) -> str:
        return (
            f"<EmailNotificationConfig(id={self.id}, name={self.name!r}, provider={self.provider})>"
        )


class WatchSourceEmail(Base):
    """Junction linking a watch source to an email notification config."""

    __tablename__ = "watch_source_email"

    id = Column(Integer, primary_key=True, index=True)
    watch_source_id = Column(
        Integer, ForeignKey("watch_source.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email_config_id = Column(
        Integer,
        ForeignKey("email_notification_config.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    additional_recipients = Column(Text, nullable=True)  # CSV
    notify_on_success = Column(Boolean, default=True, nullable=False)
    notify_on_error = Column(Boolean, default=True, nullable=False)

    watch_source = relationship("WatchSource", back_populates="email_links")
    email_config = relationship("EmailNotificationConfig", back_populates="links")

    __table_args__ = (
        UniqueConstraint("watch_source_id", "email_config_id", name="_watch_source_email_unique"),
    )

    def __repr__(self) -> str:
        return f"<WatchSourceEmail(source={self.watch_source_id}, config={self.email_config_id})>"
