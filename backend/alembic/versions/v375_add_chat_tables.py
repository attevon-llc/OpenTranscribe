"""RAG chat conversations and messages (issue #52).

Adds the two tables backing the in-app chat surface: ``chat_conversation``
(one thread, its pinned transcript scope and per-conversation settings) and
``chat_message`` (the turns, with citations and token accounting).

Design notes worth keeping with the DDL:
  - Conversations are strictly PRIVATE to their creator. ``organization_id`` is
    an isolation/billing stamp following the v372/v373 tenancy pattern, NOT a
    sharing surface — there is deliberately no share table here.
  - ``context`` pins the transcript scope (file/collection/tag selection) so a
    reopened conversation retrieves against what the user originally chose.
  - Message ``content`` and ``citations`` snippets are stored POST-redaction:
    when the owner's (or admin-forced) policy masks text before it reaches an
    LLM, the masked form is what we persist, exactly like stored summaries.
  - ``msg_metadata`` holds ids/counts/timings only — never prompt or answer
    text beyond what ``content`` already stores.
  - ``chat_message`` uses BIGSERIAL: messages accumulate far faster than any
    other row in the schema.

All SQL is idempotent (``IF NOT EXISTS``) for safe re-run on
partially-migrated databases.

Revision ID: v375_add_chat_tables
Revises: v374_add_tag_user_id
Create Date: 2026-08-04
"""

from alembic import op

revision = "v375_add_chat_tables"
down_revision = "v374_add_tag_user_id"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS chat_conversation (
            id              SERIAL PRIMARY KEY,
            uuid            UUID NOT NULL UNIQUE,
            user_id         INTEGER NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
            organization_id INTEGER REFERENCES organization(id),
            title           VARCHAR(255),
            context         JSONB,
            llm_config_id   INTEGER REFERENCES user_llm_settings(id) ON DELETE SET NULL,
            settings        JSONB,
            is_archived     BOOLEAN NOT NULL DEFAULT FALSE,
            last_message_at TIMESTAMPTZ,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """)

    # The sidebar's only query shape: this user's threads, most recent first.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chat_conversation_user_recent "
        "ON chat_conversation (user_id, last_message_at DESC NULLS LAST)"
    )
    # Partial index: community rows are all NULL, so only org rows are indexed.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chat_conversation_organization_id "
        "ON chat_conversation (organization_id) WHERE organization_id IS NOT NULL"
    )

    op.execute("""
        CREATE TABLE IF NOT EXISTS chat_message (
            id                BIGSERIAL PRIMARY KEY,
            uuid              UUID NOT NULL UNIQUE,
            conversation_id   INTEGER NOT NULL
                              REFERENCES chat_conversation(id) ON DELETE CASCADE,
            role              VARCHAR(16) NOT NULL,
            content           TEXT NOT NULL,
            citations         JSONB,
            msg_metadata      JSONB,
            prompt_tokens     INTEGER,
            completion_tokens INTEGER,
            total_tokens      INTEGER,
            tokens_estimated  BOOLEAN NOT NULL DEFAULT FALSE,
            provider          VARCHAR(50),
            model             VARCHAR(100),
            status            VARCHAR(16) NOT NULL DEFAULT 'complete',
            error             TEXT,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """)

    # Thread replay is always "this conversation, in insertion order".
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chat_message_conversation "
        "ON chat_message (conversation_id, id)"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_chat_message_conversation")
    op.execute("DROP TABLE IF EXISTS chat_message")
    op.execute("DROP INDEX IF EXISTS ix_chat_conversation_organization_id")
    op.execute("DROP INDEX IF EXISTS ix_chat_conversation_user_recent")
    op.execute("DROP TABLE IF EXISTS chat_conversation")
