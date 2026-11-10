"""``owner_since``: when a row's current owner acquired it (v471).

Mixed into every model ``services/org_content_transfer`` re-owns. NULL means the row has had one
owner since it was created; the transfer stamps the wall clock when it rewrites the owner. The
org-member erasure re-run reads ``coalesce(owner_since, <creation time>)`` so content handed to an
already-erased member is not mistaken for a restore of that member's data
(``services/erasure_org_member_rerun``).

A mixin, not one ``mapped_column`` per model: Declarative copies a mixin's column to every class
that uses it (SQLAlchemy 2.0, "Composing Mapped Hierarchies with Mixins"), so the thirteen tables
cannot drift apart.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column


class OwnerSinceMixin:
    """Adds ``owner_since TIMESTAMPTZ NULL``."""

    owner_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
