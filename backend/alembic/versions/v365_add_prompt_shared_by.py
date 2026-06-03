"""Add shared_by attribution column to summary_prompt.

Records which user flipped sharing on for a prompt (distinct from the creator,
since an admin may share another user's prompt). Nullable FK to user.id.

Revision ID: v365_add_prompt_shared_by
Revises: v364_add_content_redaction
Create Date: 2026-06-02
"""

from alembic import op

revision = "v365_add_prompt_shared_by"
down_revision = "v364_add_content_redaction"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'summary_prompt' AND column_name = 'shared_by'
            ) THEN
                ALTER TABLE summary_prompt
                    ADD COLUMN shared_by INTEGER NULL REFERENCES "user"(id);
            END IF;
        END $$;
    """)


def downgrade():
    op.execute("ALTER TABLE summary_prompt DROP COLUMN IF EXISTS shared_by")
