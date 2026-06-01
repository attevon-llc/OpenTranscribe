"""Add content-redaction columns.

Content redaction (PII / profanity / toxicity moderation):
- ``transcript_segment.redactions`` (JSONB): cached detection spans. The original
  ``text`` is never modified — masking is applied at read time from these spans.
- ``transcript_segment.toxicity`` (JSONB): segment-level toxicity scores.
- ``media_file.redaction_status`` (VARCHAR): pending | processing | done | failed.
- ``media_file.redaction_model_version`` (VARCHAR): detector model version that
  produced the cached spans (lets an admin re-index only stale files on a model upgrade).

All idempotent (``IF NOT EXISTS``) per project migration convention.

Revision ID: v364_add_content_redaction
Revises: v363_add_asr_access_key_id
Create Date: 2026-05-30
"""

from alembic import op

revision = "v364_add_content_redaction"
down_revision = "v363_add_asr_access_key_id"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'transcript_segment' AND column_name = 'redactions'
            ) THEN
                ALTER TABLE transcript_segment ADD COLUMN redactions JSONB;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'transcript_segment' AND column_name = 'toxicity'
            ) THEN
                ALTER TABLE transcript_segment ADD COLUMN toxicity JSONB;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'media_file' AND column_name = 'redaction_status'
            ) THEN
                ALTER TABLE media_file ADD COLUMN redaction_status VARCHAR;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'media_file' AND column_name = 'redaction_model_version'
            ) THEN
                ALTER TABLE media_file ADD COLUMN redaction_model_version VARCHAR;
            END IF;
            -- Pipeline timing markers for the redaction stage (benchmarking).
            IF EXISTS (SELECT 1 FROM information_schema.tables
                       WHERE table_name = 'file_pipeline_timing') THEN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'file_pipeline_timing' AND column_name = 'redaction_start_ms') THEN
                    ALTER TABLE file_pipeline_timing ADD COLUMN redaction_start_ms BIGINT;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'file_pipeline_timing' AND column_name = 'redaction_end_ms') THEN
                    ALTER TABLE file_pipeline_timing ADD COLUMN redaction_end_ms BIGINT;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'file_pipeline_timing' AND column_name = 'redaction_detectors') THEN
                    ALTER TABLE file_pipeline_timing ADD COLUMN redaction_detectors VARCHAR(128);
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'file_pipeline_timing' AND column_name = 'pii_entities_found') THEN
                    ALTER TABLE file_pipeline_timing ADD COLUMN pii_entities_found INTEGER;
                END IF;
            END IF;
        END $$;
        """
    )
    # Index so the model-upgrade re-index can find files quickly by status.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_media_file_redaction_status ON media_file(redaction_status)"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_media_file_redaction_status")
    op.execute("ALTER TABLE media_file DROP COLUMN IF EXISTS redaction_model_version")
    op.execute("ALTER TABLE media_file DROP COLUMN IF EXISTS redaction_status")
    op.execute("ALTER TABLE transcript_segment DROP COLUMN IF EXISTS toxicity")
    op.execute("ALTER TABLE transcript_segment DROP COLUMN IF EXISTS redactions")
    op.execute("ALTER TABLE file_pipeline_timing DROP COLUMN IF EXISTS pii_entities_found")
    op.execute("ALTER TABLE file_pipeline_timing DROP COLUMN IF EXISTS redaction_detectors")
    op.execute("ALTER TABLE file_pipeline_timing DROP COLUMN IF EXISTS redaction_end_ms")
    op.execute("ALTER TABLE file_pipeline_timing DROP COLUMN IF EXISTS redaction_start_ms")
