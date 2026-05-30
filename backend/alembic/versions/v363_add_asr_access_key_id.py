"""Add access_key_id to user_asr_settings (AWS dual-credential support).

AWS Transcribe needs an Access Key ID + a Secret Access Key. The secret stays in the
encrypted ``api_key`` column; the Access Key ID goes in this new column, also AES-256-GCM
encrypted (VARCHAR(200) — the ciphertext of a ~20-char key id is well under 200).
Idempotent so it is safe to re-run.

Revision ID: v363_add_asr_access_key_id
Revises: v362_add_pipeline_timing_markers
"""

from __future__ import annotations

from alembic import op

revision = "v363_add_asr_access_key_id"
down_revision = "v362_add_pipeline_timing_markers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE user_asr_settings ADD COLUMN IF NOT EXISTS access_key_id VARCHAR(200)")


def downgrade() -> None:
    op.execute("ALTER TABLE user_asr_settings DROP COLUMN IF EXISTS access_key_id")
