"""v386: share a tag with specific users and groups.

Tags had exactly two visibility tiers: yours, or the whole deployment
(``user_id IS NULL``). There was no way to give one tag to a colleague or a
team, so a shared vocabulary meant publishing to everybody or duplicating the
word per person — the duplication this feature exists to stop.

``tag_share`` mirrors ``collection_share`` deliberately: same target shape
(exactly one of ``target_user_id`` / ``target_group_id``), same CASCADE
behaviour, same partial unique indexes. Sharing is already a solved problem in
this schema, and a second, differently-shaped grant table would be a second set
of rules to keep in step.

One deliberate difference: **no ``permission`` column.** A collection share
distinguishes viewer from editor because a collection carries files you might
be allowed to change. A tag share grants *vocabulary* — you can see the tag,
filter by it, and apply it — while renaming, merging and deleting stay with the
owner (or an admin, for system tags). Adding a column the authorization code
would always read as "viewer" would be a field that lies about being a choice.

Revision ID: v386_add_tag_share
Revises: v385_drop_orphan_tables
"""

import sqlalchemy as sa

from alembic import op

revision = "v386_add_tag_share"
down_revision = "v385_drop_orphan_tables"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tag_share",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "uuid",
            sa.dialects.postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tag_id", sa.Integer(), nullable=False),
        sa.Column("shared_by_id", sa.Integer(), nullable=False),
        sa.Column("target_type", sa.String(length=20), nullable=False),
        sa.Column("target_user_id", sa.Integer(), nullable=True),
        sa.Column("target_group_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(["tag_id"], ["tag.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["shared_by_id"], ["user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_user_id"], ["user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_group_id"], ["user_group.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("uuid"),
        # Exactly one target, never both and never neither — the same guard
        # collection_share carries.
        sa.CheckConstraint(
            "(target_user_id IS NOT NULL AND target_group_id IS NULL) OR "
            "(target_user_id IS NULL AND target_group_id IS NOT NULL)",
            name="_tag_share_target_check",
        ),
    )
    op.create_index("idx_tag_share_tag_id", "tag_share", ["tag_id"])
    op.create_index("idx_tag_share_target_user_id", "tag_share", ["target_user_id"])
    op.create_index("idx_tag_share_target_group_id", "tag_share", ["target_group_id"])
    # PARTIAL uniques, not a composite one: Postgres treats NULLs as distinct,
    # so UNIQUE(tag_id, target_user_id, target_group_id) would happily admit the
    # same grant twice. collection_share is indexed the same way for the same
    # reason.
    op.create_index(
        "_tag_share_user_uc",
        "tag_share",
        ["tag_id", "target_user_id"],
        unique=True,
        postgresql_where=sa.text("target_user_id IS NOT NULL"),
    )
    op.create_index(
        "_tag_share_group_uc",
        "tag_share",
        ["tag_id", "target_group_id"],
        unique=True,
        postgresql_where=sa.text("target_group_id IS NOT NULL"),
    )


def downgrade():
    op.drop_index("_tag_share_group_uc", table_name="tag_share")
    op.drop_index("_tag_share_user_uc", table_name="tag_share")
    op.drop_index("idx_tag_share_target_group_id", table_name="tag_share")
    op.drop_index("idx_tag_share_target_user_id", table_name="tag_share")
    op.drop_index("idx_tag_share_tag_id", table_name="tag_share")
    op.drop_table("tag_share")
