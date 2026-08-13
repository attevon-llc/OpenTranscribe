"""SQLAlchemy models for user groups, group membership and directory mappings."""

import uuid as uuid_pkg
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import UniqueConstraint
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base
from app.utils.uuid7 import uuid7

if TYPE_CHECKING:
    from app.models.sharing import CollectionShare
    from app.models.user import User

#: Directory sources a :class:`GroupMapping` can key off. ``ldap`` claim values are
#: group DNs as the server returns them; ``oidc`` values are whatever the configured
#: roles claim emits (realm roles, Authentik/Okta groups, Entra app roles); ``proxy``
#: values are the names an authenticating reverse proxy puts in its groups header
#: (``v380``). They are deliberately separate namespaces — a DN and a role name are
#: not interchangeable, and their matching rules differ (see the mapping service).
MAPPING_SOURCE_LDAP = "ldap"
MAPPING_SOURCE_OIDC = "oidc"
MAPPING_SOURCE_PROXY = "proxy"
MAPPING_SOURCES = (MAPPING_SOURCE_LDAP, MAPPING_SOURCE_OIDC, MAPPING_SOURCE_PROXY)

#: ``user_group_member.source``. Every row that predates ``v376`` is ``manual`` and
#: must stay that way: reconciliation removes only what a directory pass added, so
#: this value is the difference between "revocation works" and "a sync wipes the
#: teams somebody built by hand". ``scim`` marks a membership an identity provider
#: wrote through ``/scim/v2/Groups`` (``v380``) and is protected for the same reason:
#: the provisioning system that created it is the one that gets to take it away.
MEMBERSHIP_SOURCE_MANUAL = "manual"
MEMBERSHIP_SOURCE_SCIM = "scim"
MEMBERSHIP_SOURCES = (
    MEMBERSHIP_SOURCE_MANUAL,
    MEMBERSHIP_SOURCE_SCIM,
    MAPPING_SOURCE_LDAP,
    MAPPING_SOURCE_OIDC,
    MAPPING_SOURCE_PROXY,
)

#: Membership sources a directory reconciliation pass must never touch. Everything
#: else in :data:`MEMBERSHIP_SOURCES` is claim-derived and owned by the next pass.
MEMBERSHIP_SOURCES_PROTECTED = (MEMBERSHIP_SOURCE_MANUAL, MEMBERSHIP_SOURCE_SCIM)

#: The CHECK bodies, as SQL. Kept beside the tuples so the model and ``v380``'s
#: constraint swap cannot drift — the consistency test compares the live constraint
#: against these strings.
MEMBERSHIP_SOURCES_SQL = ", ".join(f"'{s}'" for s in MEMBERSHIP_SOURCES)
MAPPING_SOURCES_SQL = ", ".join(f"'{s}'" for s in MAPPING_SOURCES)


class UserGroup(Base):
    """User-created group for sharing collections."""

    __tablename__ = "user_group"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Tenant stamp (v388). NULL = personal scope, exactly like every other org-stamped
    #: table; the community edition leaves it NULL for every row because
    #: `organization_membership` is empty there. No `ondelete`, matching the other 11 FKs
    #: into `organization`: deleting a tenant must not silently strip rows of their
    #: tenancy and re-expose them as personal data.
    organization_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("organization.id"), nullable=True
    )
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("owner_id", "name", name="_user_group_owner_name_uc"),)

    # Relationships
    owner: Mapped["User"] = relationship("User", back_populates="owned_groups")
    members: Mapped[list["UserGroupMember"]] = relationship(
        "UserGroupMember", back_populates="group", cascade="all, delete-orphan"
    )
    collection_shares: Mapped[list["CollectionShare"]] = relationship(
        "CollectionShare",
        back_populates="target_group",
        foreign_keys="CollectionShare.target_group_id",
        cascade="all, delete-orphan",
    )
    mappings: Mapped[list["GroupMapping"]] = relationship(
        "GroupMapping", back_populates="user_group", cascade="all, delete-orphan"
    )


class UserGroupMember(Base):
    """Membership record linking users to groups with roles."""

    __tablename__ = "user_group_member"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    group_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user_group.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(
        String(20), nullable=False, default="member"
    )  # "owner", "admin", "member"
    #: Who put this row here. ``manual`` (a human, through the groups UI) and ``scim``
    #: (a provisioning system, through /scim/v2/Groups) are protected — no directory
    #: pass may remove them. ``ldap``/``oidc``/``proxy`` mean a mapping produced the
    #: row and the next reconciliation owns its lifetime.
    source: Mapped[str] = mapped_column(
        String(20), nullable=False, default=MEMBERSHIP_SOURCE_MANUAL, server_default="manual"
    )
    joined_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("group_id", "user_id", name="_group_member_uc"),
        CheckConstraint(
            f"source IN ({MEMBERSHIP_SOURCES_SQL})", name="ck_user_group_member_source_valid"
        ),
        # v211. Enforced by the database since v211 and declared nowhere until
        # now — the sibling source CHECK directly above it is exactly what made
        # its absence read as deliberate. Literal rather than derived: there is
        # no Python-side tuple for these three, and inventing one here would put
        # a second source of truth beside the DDL.
        CheckConstraint(
            "role IN ('owner', 'admin', 'member')", name="_user_group_member_role_check"
        ),
    )

    # Relationships
    group: Mapped["UserGroup"] = relationship("UserGroup", back_populates="members")
    user: Mapped["User"] = relationship("User", back_populates="group_memberships")


class GroupMapping(Base):
    """One directory claim value bound to an in-app group and/or a privilege.

    This is the row that turns ``CN=Legal-Team,OU=Groups,DC=corp,DC=example`` into
    membership of the OpenTranscribe group "Legal", and optionally into ``admin``.
    Both halves are optional but at least one must be present, or the mapping does
    nothing (``ck_group_mapping_grants_something``).

    ``grants_role`` is capped at ``admin`` by ``ck_group_mapping_role_capped``.
    ``super_admin`` is unreachable from any IdP on purpose: it is the break-glass
    account for the very directory that might be misconfigured, so no directory may
    mint one. The service layer enforces the same cap before it ever reaches the DB.

    Deleting the target :class:`UserGroup` cascades the mapping away rather than
    leaving it as a role-only grant nobody can see in the groups UI.
    """

    __tablename__ = "group_mapping"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    source: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    #: The group/role string exactly as the IdP emits it. Long, because an AD DN is.
    claim_value: Mapped[str] = mapped_column(String(1024), nullable=False)
    user_group_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user_group.id", ondelete="CASCADE"), nullable=True, index=True
    )
    grants_role: Mapped[str | None] = mapped_column(String(20), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("source", "claim_value", name="uq_group_mapping_source_claim"),
        CheckConstraint(f"source IN ({MAPPING_SOURCES_SQL})", name="ck_group_mapping_source_valid"),
        CheckConstraint(
            "grants_role IS NULL OR grants_role IN ('user', 'admin')",
            name="ck_group_mapping_role_capped",
        ),
        CheckConstraint(
            "user_group_id IS NOT NULL OR grants_role IS NOT NULL",
            name="ck_group_mapping_grants_something",
        ),
        # Expression AND partial: UNIQUE (lower(claim_value)) WHERE source='ldap'.
        # An LDAP DN is case-insensitive, so two mappings differing only in case
        # are the same mapping; OIDC/proxy claim values are case-sensitive and are
        # covered by ``uq_group_mapping_source_claim`` above.
        # ``api/endpoints/admin_group_mappings.py`` catches this index's
        # IntegrityError to return 409 rather than 500 — the rule is load-bearing
        # in the API, and was declared only in the DDL.
        Index(
            "uq_group_mapping_ldap_claim_ci",
            text("lower(claim_value)"),
            unique=True,
            postgresql_where=text("source = 'ldap'"),
        ),
    )

    # Relationships
    user_group: Mapped["UserGroup | None"] = relationship("UserGroup", back_populates="mappings")
