"""Chat projects: grouped conversations with pinned scope and prompt (issue #360).

A project is a workspace for one recurring subject — a client, a weekly
meeting, a case. It carries two things a bare folder cannot:

  - ``scope`` pins a default transcript selection, so every chat created inside
    the project retrieves from that client's recordings without the user
    re-picking context each time. This is the part that makes projects a
    *retrieval boundary* rather than filing, and it is why they fit here better
    than in a general-purpose chat product.
  - ``system_prompt`` is prompt layer 3, sitting between the user's account-wide
    default and a per-conversation override. It carries standing background
    ("this client calls their product Atlas") without re-typing it per chat.

Design notes worth keeping with the DDL:
  - ``chat_conversation.project_id`` is NULLABLE and defaults to NULL, so every
    existing conversation keeps working exactly as before, ungrouped.
  - The FK is ``ON DELETE SET NULL``, NOT CASCADE. Deleting a project must never
    destroy the conversations inside it — they fall back to ungrouped, which is
    recoverable, whereas a cascade is not.
  - Projects are strictly PRIVATE to their creator, matching chat_conversation.
    ``organization_id`` is an isolation/billing stamp following the v372/v373
    tenancy pattern, not a sharing surface.
  - ``scope`` is JSONB in the same shape as ``chat_conversation.context`` so the
    existing resolver reads either without a second code path.

All SQL is idempotent (``IF NOT EXISTS``) for safe re-run on
partially-migrated databases.

Revision ID: v376_add_chat_projects
Revises: v375_add_chat_tables
Create Date: 2026-08-07
"""

from alembic import op

revision = "v376_add_chat_projects"
down_revision = "v375_add_chat_tables"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS chat_project (
            id              SERIAL PRIMARY KEY,
            uuid            UUID NOT NULL UNIQUE,
            user_id         INTEGER NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
            organization_id INTEGER REFERENCES organization(id),
            name            VARCHAR(120) NOT NULL,
            description     TEXT,
            system_prompt   TEXT,
            scope           JSONB,
            llm_config_id   INTEGER REFERENCES user_llm_settings(id) ON DELETE SET NULL,
            is_archived     BOOLEAN NOT NULL DEFAULT FALSE,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """)

    # The sidebar's only query shape: this user's projects, alphabetical.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chat_project_user_name ON chat_project (user_id, name)"
    )
    # Partial index: community rows are all NULL, so only org rows are indexed.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chat_project_organization_id "
        "ON chat_project (organization_id) WHERE organization_id IS NOT NULL"
    )

    # SET NULL, not CASCADE — see the module docstring.
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'chat_conversation' AND column_name = 'project_id'
            ) THEN
                ALTER TABLE chat_conversation
                    ADD COLUMN project_id INTEGER
                    REFERENCES chat_project(id) ON DELETE SET NULL;
            END IF;
        END $$;
    """)

    # Grouping the sidebar: this user's threads within one project, newest first.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chat_conversation_project "
        "ON chat_conversation (project_id, last_message_at DESC NULLS LAST) "
        "WHERE project_id IS NOT NULL"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_chat_conversation_project")
    op.execute("ALTER TABLE chat_conversation DROP COLUMN IF EXISTS project_id")
    op.execute("DROP INDEX IF EXISTS ix_chat_project_organization_id")
    op.execute("DROP INDEX IF EXISTS ix_chat_project_user_name")
    op.execute("DROP TABLE IF EXISTS chat_project")
