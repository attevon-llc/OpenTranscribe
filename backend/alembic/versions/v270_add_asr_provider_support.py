"""Add multi-provider ASR support — user_asr_settings, custom_vocabulary tables, new media_file/transcript_segment columns.

Revision ID: v270_add_asr_provider_support
Revises: v270_add_profile_avatar
Create Date: 2026-03-05
"""

from alembic import op

revision = "v270_add_asr_provider_support"
down_revision = "v270_add_profile_avatar"
branch_labels = None
depends_on = None

_MEDIA_FILE_COLS = [
    ("media_file", "asr_provider", "VARCHAR"),
    ("media_file", "asr_model", "VARCHAR"),
    ("media_file", "diarization_provider", "VARCHAR"),
    ("transcript_segment", "confidence", "FLOAT"),
]


def upgrade():
    # Add ASR-tracking columns to existing tables
    for table, column, col_type in _MEDIA_FILE_COLS:
        op.execute(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = '{table}' AND column_name = '{column}'
                ) THEN
                    ALTER TABLE {table} ADD COLUMN {column} {col_type};
                END IF;
            END $$;
            """  # nosec B608
        )

    # user_asr_settings table
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS user_asr_settings (
            id          SERIAL PRIMARY KEY,
            uuid        UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
            user_id     INTEGER NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
            name        VARCHAR(100) NOT NULL,
            provider    VARCHAR(50) NOT NULL,
            model_name  VARCHAR(100) NOT NULL,
            api_key     TEXT,
            base_url    VARCHAR(500),
            region      VARCHAR(50),
            is_active   BOOLEAN NOT NULL DEFAULT TRUE,
            last_tested TIMESTAMPTZ,
            test_status VARCHAR(20),
            test_message TEXT,
            created_at  TIMESTAMPTZ DEFAULT NOW(),
            updated_at  TIMESTAMPTZ DEFAULT NOW(),
            CONSTRAINT _user_asr_config_name_unique UNIQUE (user_id, name)
        );
        CREATE INDEX IF NOT EXISTS ix_user_asr_settings_uuid        ON user_asr_settings(uuid);
        CREATE INDEX IF NOT EXISTS ix_user_asr_settings_user_id     ON user_asr_settings(user_id);
        CREATE INDEX IF NOT EXISTS ix_user_asr_settings_provider    ON user_asr_settings(provider);
        CREATE INDEX IF NOT EXISTS ix_user_asr_settings_user_prov   ON user_asr_settings(user_id, provider);
    """
    )

    # custom_vocabulary table — generalised domain vocabulary boosting
    # Supports: medical, legal, corporate, government, technical, general
    # Works with: Deepgram keywords, AWS custom vocab, Speechmatics additional_vocab,
    #             Gladia custom_vocabulary, AND local faster-whisper hotwords
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS custom_vocabulary (
            id          SERIAL PRIMARY KEY,
            user_id     INTEGER REFERENCES "user"(id) ON DELETE CASCADE,
            term        VARCHAR(200) NOT NULL,
            domain      VARCHAR(50) NOT NULL DEFAULT 'general',
            category    VARCHAR(100),
            is_active   BOOLEAN NOT NULL DEFAULT TRUE,
            created_at  TIMESTAMPTZ DEFAULT NOW(),
            updated_at  TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE UNIQUE INDEX IF NOT EXISTS _custom_vocab_unique ON custom_vocabulary (COALESCE(user_id, 0), term, domain);
        CREATE INDEX IF NOT EXISTS ix_custom_vocabulary_user_id       ON custom_vocabulary(user_id);
        CREATE INDEX IF NOT EXISTS ix_custom_vocabulary_domain        ON custom_vocabulary(domain);
        CREATE INDEX IF NOT EXISTS ix_custom_vocabulary_user_active   ON custom_vocabulary(user_id, is_active);
    """
    )


def downgrade():
    op.execute("DROP TABLE IF EXISTS custom_vocabulary")
    op.execute("DROP TABLE IF EXISTS user_asr_settings")
    for table, column, _ in reversed(_MEDIA_FILE_COLS):
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")
