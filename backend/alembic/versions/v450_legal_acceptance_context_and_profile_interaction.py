"""Legal acceptance context (organization, user agent, locale, scope) and profile interaction time.

Why
---
An acceptance record must say more than "user X accepted text Y": the organization the user
was acting in, the client that submitted it and the language it was shown in. An org admin can
now also accept a document **on behalf of** an organization (``scope = 'organization'``), so an
organization's evidence is its own row rather than an inference from a member's.

Org-scope rows have their own partial UNIQUE index (one per organization per exact text). The
old ``UNIQUE(user_id, document_key, sha256)`` also covered them, which made an admin of two
organizations unable to accept the same text for both, so it becomes a partial UNIQUE INDEX
(``WHERE scope = 'user'``) with the same name. Every pre-existing row has ``scope = 'user'``
and already satisfies it, so this is safe on a populated table.

``speaker_profile.last_interaction_at`` is when a person last linked, unlinked, edited or decided
on this voiceprint (``app/services/speaker_profile_activity.py`` defines the term). It is
backfilled from the existing timestamps. ``updated_at`` cannot serve: maintenance bumps it.

Affects every deployment; purely additive (nullable columns or a column with a default).
Rollback by image works without a downgrade: older code ignores the new columns, and the only
changed constraint became strictly more permissive.

Downgrade deletes data: org-scope acceptances, and duplicate user-scope rows (the oldest is
kept) that the restored plain UNIQUE constraint would reject. It is not executed by any test
against real data.

Revision ID: v450_legal_acceptance_context_and_profile_interaction
Revises: v449_add_speaker_identity_source
Create Date: 2026-11-01
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v450_legal_acceptance_context_and_profile_interaction"
down_revision = "v449_add_speaker_identity_source"
branch_labels = None
depends_on = None

#: Module-level so the consistency test replays the real statements.
UPGRADE_SQL = """
    ALTER TABLE legal_acceptance ADD COLUMN IF NOT EXISTS scope VARCHAR(16) NOT NULL DEFAULT 'user';
    ALTER TABLE legal_acceptance ADD COLUMN IF NOT EXISTS organization_id INTEGER
        REFERENCES organization(id) ON DELETE SET NULL;
    ALTER TABLE legal_acceptance ADD COLUMN IF NOT EXISTS organization_uuid UUID;
    ALTER TABLE legal_acceptance ADD COLUMN IF NOT EXISTS user_agent VARCHAR(512);
    ALTER TABLE legal_acceptance ADD COLUMN IF NOT EXISTS locale VARCHAR(35);
    DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conname = 'uq_legal_acceptance_user_doc_sha' AND contype = 'u') THEN
        ALTER TABLE legal_acceptance DROP CONSTRAINT uq_legal_acceptance_user_doc_sha;
      END IF;
    END $$;
    CREATE UNIQUE INDEX IF NOT EXISTS uq_legal_acceptance_user_doc_sha
        ON legal_acceptance (user_id, document_key, sha256) WHERE scope = 'user';
    CREATE UNIQUE INDEX IF NOT EXISTS uq_legal_acceptance_org_doc_sha
        ON legal_acceptance (organization_uuid, document_key, sha256) WHERE scope = 'organization';
    CREATE INDEX IF NOT EXISTS ix_legal_acceptance_organization_id
        ON legal_acceptance (organization_id);

    ALTER TABLE speaker_profile ADD COLUMN IF NOT EXISTS last_interaction_at TIMESTAMPTZ;
    UPDATE speaker_profile
       SET last_interaction_at = GREATEST(created_at, last_embedding_update, updated_at)
     WHERE last_interaction_at IS NULL;
    CREATE INDEX IF NOT EXISTS ix_speaker_profile_last_interaction_at
        ON speaker_profile (last_interaction_at);

    -- Created last: the detection arm keys on it.
    DO $$ BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_legal_acceptance_scope') THEN
        ALTER TABLE legal_acceptance ADD CONSTRAINT ck_legal_acceptance_scope CHECK (
          (scope = 'user') OR (scope = 'organization' AND organization_uuid IS NOT NULL));
      END IF;
    END $$;
"""

DOWNGRADE_SQL = """
    DROP INDEX IF EXISTS ix_speaker_profile_last_interaction_at;
    ALTER TABLE speaker_profile DROP COLUMN IF EXISTS last_interaction_at;

    ALTER TABLE legal_acceptance DROP CONSTRAINT IF EXISTS ck_legal_acceptance_scope;
    DROP INDEX IF EXISTS ix_legal_acceptance_organization_id;
    DROP INDEX IF EXISTS uq_legal_acceptance_org_doc_sha;
    DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_name = 'legal_acceptance' AND column_name = 'scope') THEN
        DELETE FROM legal_acceptance WHERE scope = 'organization';
      END IF;
    END $$;
    DELETE FROM legal_acceptance a USING legal_acceptance b
     WHERE a.user_id = b.user_id AND a.document_key = b.document_key AND a.sha256 = b.sha256
       AND (a.accepted_at, a.id) > (b.accepted_at, b.id);
    DO $$ BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_legal_acceptance_user_doc_sha') THEN
        DROP INDEX IF EXISTS uq_legal_acceptance_user_doc_sha;
        ALTER TABLE legal_acceptance ADD CONSTRAINT uq_legal_acceptance_user_doc_sha
          UNIQUE (user_id, document_key, sha256);
      END IF;
    END $$;
    ALTER TABLE legal_acceptance DROP COLUMN IF EXISTS locale;
    ALTER TABLE legal_acceptance DROP COLUMN IF EXISTS user_agent;
    ALTER TABLE legal_acceptance DROP COLUMN IF EXISTS organization_uuid;
    ALTER TABLE legal_acceptance DROP COLUMN IF EXISTS organization_id;
    ALTER TABLE legal_acceptance DROP COLUMN IF EXISTS scope;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
