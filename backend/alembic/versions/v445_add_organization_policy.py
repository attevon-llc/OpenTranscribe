"""Add the per-organization privacy policy and redaction floor (issues #1280, #1279).

Why
---
Voiceprint matching and inferred gender are governed by one restrict-only policy layer
(``app.services.privacy.policy``): platform, instance, organization and user are ANDed, and
any layer that is off turns the feature off. The organization layer needs somewhere to live.
The same row also stores the organization's redaction floor, which core unions with whatever
a registered floor resolver returns (``core.tenant_limits.resolve_redaction_floor``).

What
----
One row per organization in ``organization_policy``; a missing row means "inherit
everything" (both switches on, empty floor). The switches default TRUE so that creating a row
for some other reason never turns a feature off. ``organization_id`` is the primary key and
``ON DELETE CASCADE``: the row is configuration owned by the organization, not evidence.
``updated_by_user_id`` is ``ON DELETE SET NULL`` so deleting an administrator's account keeps
the policy and drops only the attribution.

The three floor collections are JSONB arrays guarded by ``jsonb_typeof`` CHECKs and the
toxicity threshold by a range CHECK, so a bad write fails in the database and not only in the
API validation that sits in front of it.

Community edition has no organizations, so the table stays empty. Upgrade is
behaviour-neutral: no rows, no change to any feature.

Downgrade drops the table and with it every organization's saved choices. The instance-wide
``privacy.*`` settings are ordinary ``system_settings`` rows and are left alone.

Revision ID: v445_add_organization_policy
Revises: v444_add_takedown_notification
Create Date: 2026-10-25
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v445_add_organization_policy"
down_revision = "v444_add_takedown_notification"
branch_labels = None
depends_on = None

#: Module-level so the consistency test replays the real statements.
UPGRADE_SQL = """
    CREATE TABLE IF NOT EXISTS organization_policy (
        organization_id INTEGER PRIMARY KEY REFERENCES organization(id) ON DELETE CASCADE,
        voiceprint_matching_enabled BOOLEAN NOT NULL DEFAULT TRUE,
        gender_inference_enabled BOOLEAN NOT NULL DEFAULT TRUE,
        floor_forced_categories JSONB NOT NULL DEFAULT '[]'::jsonb,
        floor_forced_pii_entities JSONB NOT NULL DEFAULT '[]'::jsonb,
        floor_forced_custom_words JSONB NOT NULL DEFAULT '[]'::jsonb,
        floor_toxicity_threshold DOUBLE PRECISION,
        floor_export_redacted BOOLEAN NOT NULL DEFAULT FALSE,
        floor_redact_before_llm BOOLEAN NOT NULL DEFAULT FALSE,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_by_user_id INTEGER REFERENCES "user"(id) ON DELETE SET NULL
    );

    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'ck_organization_policy_floor_toxicity_range'
        ) THEN
            ALTER TABLE organization_policy
                ADD CONSTRAINT ck_organization_policy_floor_toxicity_range
                CHECK (floor_toxicity_threshold IS NULL
                       OR (floor_toxicity_threshold >= 0 AND floor_toxicity_threshold <= 1));
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'ck_organization_policy_floor_categories_array'
        ) THEN
            ALTER TABLE organization_policy
                ADD CONSTRAINT ck_organization_policy_floor_categories_array
                CHECK (jsonb_typeof(floor_forced_categories) = 'array');
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'ck_organization_policy_floor_pii_entities_array'
        ) THEN
            ALTER TABLE organization_policy
                ADD CONSTRAINT ck_organization_policy_floor_pii_entities_array
                CHECK (jsonb_typeof(floor_forced_pii_entities) = 'array');
        END IF;
        -- Created last: the detection arm keys on it.
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'ck_organization_policy_floor_custom_words_array'
        ) THEN
            ALTER TABLE organization_policy
                ADD CONSTRAINT ck_organization_policy_floor_custom_words_array
                CHECK (jsonb_typeof(floor_forced_custom_words) = 'array');
        END IF;
    END $$;
"""

DOWNGRADE_SQL = """
    DROP TABLE IF EXISTS organization_policy;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
