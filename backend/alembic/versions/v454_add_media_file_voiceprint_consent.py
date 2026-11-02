"""Per-file voiceprint consent: ``media_file.voiceprint_consent`` and its record.

Why
---
An uploader can state, per recording, whether the speakers agreed to voice identification, and the
owner can opt a file out later (issue #1356). The state is a typed column rather than a JSONB key:
the privacy gate reads it on every decision, a CHECK makes an invalid state unrepresentable, and
``metadata_important`` is extraction output that reprocessing rewrites.

``unspecified`` (the default, and what every existing file becomes) changes nothing. ``declined``
denies voiceprints for the file. ``granted`` only records the uploader's claim; it never enables
anything an instance, organization or user switch turned off. The columns record the uploader's
ASSERTION and who made it; the product does not verify it.

Safe on a populated table: ``ADD COLUMN ... NOT NULL DEFAULT '<constant>'`` does not rewrite on
Postgres 11+, and every statement is idempotent.

Downgrade REFUSES while any file is ``declined`` unless ``OT_ALLOW_CONSENT_DOWNGRADE=1``: an older
build ignores the column and would start creating voiceprints for opted-out files.

Revision ID: v454_add_media_file_voiceprint_consent
Revises: v451_legal_acceptance_survives_user_deletion
Create Date: 2026-11-01
"""

import os

from alembic import op

# revision identifiers, used by Alembic.
revision = "v454_add_media_file_voiceprint_consent"
down_revision = "v451_legal_acceptance_survives_user_deletion"
branch_labels = None
depends_on = None

#: Frozen copy of ``models.media.VOICEPRINT_CONSENT_STATES``; the consistency test asserts equality.
STATES = ("unspecified", "granted", "declined")

UPGRADE_SQL = f"""
    ALTER TABLE media_file
        ADD COLUMN IF NOT EXISTS voiceprint_consent VARCHAR(16) NOT NULL DEFAULT 'unspecified';
    ALTER TABLE media_file ADD COLUMN IF NOT EXISTS voiceprint_consent_at TIMESTAMPTZ;
    ALTER TABLE media_file ADD COLUMN IF NOT EXISTS voiceprint_consent_by_user_id INTEGER;

    DO $$
    BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_constraint
                      WHERE conname = 'fk_media_file_voiceprint_consent_by_user') THEN
        ALTER TABLE media_file ADD CONSTRAINT fk_media_file_voiceprint_consent_by_user
            FOREIGN KEY (voiceprint_consent_by_user_id) REFERENCES "user"(id) ON DELETE SET NULL;
      END IF;
      IF NOT EXISTS (SELECT 1 FROM pg_constraint
                      WHERE conname = 'ck_media_file_voiceprint_consent') THEN
        ALTER TABLE media_file ADD CONSTRAINT ck_media_file_voiceprint_consent
            CHECK (voiceprint_consent IN ({", ".join(f"'{s}'" for s in STATES)}));
      END IF;
    END $$;

    CREATE INDEX IF NOT EXISTS ix_media_file_voiceprint_declined
        ON media_file (id) WHERE voiceprint_consent = 'declined';
"""

DOWNGRADE_SQL = """
    DROP INDEX IF EXISTS ix_media_file_voiceprint_declined;
    ALTER TABLE media_file DROP CONSTRAINT IF EXISTS ck_media_file_voiceprint_consent;
    ALTER TABLE media_file DROP CONSTRAINT IF EXISTS fk_media_file_voiceprint_consent_by_user;
    ALTER TABLE media_file DROP COLUMN IF EXISTS voiceprint_consent_by_user_id;
    ALTER TABLE media_file DROP COLUMN IF EXISTS voiceprint_consent_at;
    ALTER TABLE media_file DROP COLUMN IF EXISTS voiceprint_consent;
"""

REFUSAL_QUERY = "SELECT count(*) FROM media_file WHERE voiceprint_consent = 'declined'"


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    bind = op.get_bind()
    has_column = bind.exec_driver_sql(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'media_file' AND column_name = 'voiceprint_consent'"
    ).first()
    if has_column and os.environ.get("OT_ALLOW_CONSENT_DOWNGRADE") != "1":
        declined = bind.exec_driver_sql(REFUSAL_QUERY).scalar()
        if declined:
            raise RuntimeError(
                f"{declined} file(s) are opted out of voice identification; downgrading would "
                "drop that state and an older build would create voiceprints for them. Set "
                "OT_ALLOW_CONSENT_DOWNGRADE=1 to proceed anyway."
            )
    op.execute(DOWNGRADE_SQL)
