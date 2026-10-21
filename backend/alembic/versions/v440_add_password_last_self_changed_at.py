"""Track when the account holder last chose their own password.

Why
---
``user.password_changed_at`` answers "when was the current credential set" and drives
expiry. The minimum-age control (FedRAMP IA-5(1)(d)) asks something different: when did
the HOLDER last choose a password. The bootstrap admin is seeded with
``password_changed_at = now``, so the minimum-age check refused the very first deliberate
change the first-run wizard asks for, for 24 hours.

``password_last_self_changed_at`` is set only by a successful self-service change. NULL
means the holder has never chosen one, so the first change is allowed. No backfill: an
existing account's first self-service change is simply free, once.

Revision ID: v440_add_password_last_self_changed_at
Revises: v439_infer_legacy_voiceprint_suggestion_source
Create Date: 2026-10-20
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v440_add_password_last_self_changed_at"
down_revision = "v439_infer_legacy_voiceprint_suggestion_source"
branch_labels = None
depends_on = None

#: Module-level so the consistency test replays the real statements.
UPGRADE_SQL = """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'user' AND column_name = 'password_last_self_changed_at'
        ) THEN
            ALTER TABLE "user"
                ADD COLUMN password_last_self_changed_at TIMESTAMP WITH TIME ZONE;
        END IF;
    END $$;
"""

DOWNGRADE_SQL = """
    ALTER TABLE "user" DROP COLUMN IF EXISTS password_last_self_changed_at;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
