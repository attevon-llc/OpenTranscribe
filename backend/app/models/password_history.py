"""
PasswordHistory model for tracking user password changes.

Stores previous password hashes to prevent password reuse.
Part of FedRAMP IA-5 compliance for password history.
"""

import uuid as uuid_pkg
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.user import User


class PasswordHistory(Base):
    """
    Model for storing password history records.

    Each record represents a previously used password hash for a user.
    Used to enforce password reuse prevention (FedRAMP IA-5).

    Attributes:
        id: Primary key
        uuid: Unique identifier for external reference
        user_id: Foreign key to user table
        password_hash: The hashed password (bcrypt or PBKDF2)
        created_at: When this password was set
    """

    __tablename__ = "password_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True),
        unique=True,
        nullable=False,
        default=uuid_pkg.uuid4,
        index=True,
    )
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    # Relationship to user
    user: Mapped["User"] = relationship("User", back_populates="password_history")
