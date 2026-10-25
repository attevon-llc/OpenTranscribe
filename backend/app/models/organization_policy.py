"""Per-organization privacy policy and redaction floor (migration v445, issues #1280, #1279).

One row per organization; a missing row means "inherit everything" (both switches on, empty
floor). Community edition has no organizations, so the table stays empty there.

The switches are restrict-only: the effective answer is platform AND instance AND organization
AND user (``app.services.privacy.policy``), so an organization can turn a feature off for its
members but can never turn on what the deployment's operator turned off.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean
from sqlalchemy import CheckConstraint
from sqlalchemy import DateTime
from sqlalchemy import Float
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.sql import func

from app.db.base import Base


class OrganizationPolicy(Base):
    """An organization's privacy switches and redaction floor."""

    __tablename__ = "organization_policy"
    __table_args__ = (
        CheckConstraint(
            "floor_toxicity_threshold IS NULL "
            "OR (floor_toxicity_threshold >= 0 AND floor_toxicity_threshold <= 1)",
            name="ck_organization_policy_floor_toxicity_range",
        ),
        CheckConstraint(
            "jsonb_typeof(floor_forced_categories) = 'array'",
            name="ck_organization_policy_floor_categories_array",
        ),
        CheckConstraint(
            "jsonb_typeof(floor_forced_pii_entities) = 'array'",
            name="ck_organization_policy_floor_pii_entities_array",
        ),
        CheckConstraint(
            "jsonb_typeof(floor_forced_custom_words) = 'array'",
            name="ck_organization_policy_floor_custom_words_array",
        ),
    )

    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organization.id", ondelete="CASCADE"), primary_key=True
    )
    voiceprint_matching_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    gender_inference_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    floor_forced_categories: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    floor_forced_pii_entities: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    floor_forced_custom_words: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    floor_toxicity_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    floor_export_redacted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    floor_redact_before_llm: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    updated_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
