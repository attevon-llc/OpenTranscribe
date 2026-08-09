"""SQLAlchemy models for watch-source email notifications.

``EmailNotificationConfig`` holds a reusable, admin-managed email provider
configuration (SMTP / Microsoft 365 Graph / Exchange on-prem) with secrets
AES-256-GCM encrypted. ``WatchSourceEmail`` is the junction linking a watch
source to one or more email configs, with per-link recipient overrides and
success/error toggles.

These rows are **deployment-wide super_admin infrastructure**, not user-owned:
``created_by`` records who added the row, and the whole CRUD surface in
``api/endpoints/watch_sources.py`` is gated on ``get_current_active_superuser``.

One row may additionally be designated to carry **transactional auth mail**
(password reset, invitation, address verification, security notice). That
designation lives in the ``SystemSettings`` key ``email.auth_config_uuid``
(``core/constants.py: AUTH_EMAIL_CONFIG_SETTING_KEY``) rather than in a column
here — it is a property of the deployment, not of the provider, so encoding it as
a column would mean a migration plus a "only one row may be true" invariant with
no DB constraint able to express it. Resolution lives in
``services/email_service.load_auth_mail_config``; the designation is written and
validated in ``services/auth_mail_config_service`` and exposed at
``PUT /api/admin/auth-config/email/designation``.

The CRUD routes **refuse** (409) to delete or disable the designated row, since
either would silently reroute password resets to the ``SMTP_*`` env transport —
unset in every stock deployment. Should the row vanish another way, the read path
degrades to that env transport with an error log, never to a different config.
"""

import uuid as uuid_pkg
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base
from app.utils.uuid7 import uuid7

if TYPE_CHECKING:
    from app.models.watch_source import WatchSource


class EmailNotificationConfig(Base):
    """A reusable email provider configuration for notifications."""

    __tablename__ = "email_notification_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)  # smtp | m365 | exchange

    # ----- SMTP -----
    smtp_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    smtp_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    smtp_use_tls: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    smtp_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    encrypted_smtp_password: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # AES-256-GCM encrypted

    # ----- Microsoft 365 (Graph OAuth2) -----
    m365_tenant_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    m365_client_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    encrypted_m365_client_secret: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # AES-256-GCM encrypted

    # ----- Exchange on-prem -----
    exchange_server: Mapped[str | None] = mapped_column(String(255), nullable=True)
    exchange_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    exchange_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    encrypted_exchange_password: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # AES-256-GCM encrypted

    # ----- common -----
    from_address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    default_recipients: Mapped[str | None] = mapped_column(Text, nullable=True)  # CSV
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    test_status: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )  # success | failed | untested
    test_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    links: Mapped[list["WatchSourceEmail"]] = relationship(
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

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    watch_source_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("watch_source.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email_config_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("email_notification_config.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    additional_recipients: Mapped[str | None] = mapped_column(Text, nullable=True)  # CSV
    notify_on_success: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notify_on_error: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    watch_source: Mapped["WatchSource"] = relationship("WatchSource", back_populates="email_links")
    email_config: Mapped["EmailNotificationConfig"] = relationship(
        "EmailNotificationConfig", back_populates="links"
    )

    __table_args__ = (
        UniqueConstraint("watch_source_id", "email_config_id", name="_watch_source_email_unique"),
    )

    def __repr__(self) -> str:
        return f"<WatchSourceEmail(source={self.watch_source_id}, config={self.email_config_id})>"
