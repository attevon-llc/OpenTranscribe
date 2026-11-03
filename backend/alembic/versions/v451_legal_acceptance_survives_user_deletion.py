"""Legal acceptance records outlive the account that made them, pseudonymised.

Why
---
An acceptance is evidence of which terms applied, and it is needed most after the person who
accepted has left: an organization's acceptance must outlive the admin who clicked, and a
dispute about which text a deleted user accepted can arise long after the deletion. Until now
``legal_acceptance.user_id`` was ``ON DELETE CASCADE``, so every deletion path (admin delete,
GDPR erasure, the first-run wizard's raw ``DELETE FROM "user"``) destroyed the evidence.

After this revision a deleted user's rows stay, **pseudonymised, not anonymous**:

* ``user_id`` -> NULL (``ON DELETE SET NULL``), ``ip_address`` and ``user_agent`` -> NULL;
* ``subject_user_uuid`` keeps the account's surrogate uuid (the erasure-ledger model: it means
  nothing until a backup restores the account, so the row is pseudonymised personal data);
* ``subject_erased_at`` = the deletion time, ``retain_until`` = that plus
  ``legal.acceptance_retention_days`` (``system_settings``; default 2190 = 6 years; floor 365).
  The retention sweep (``app/tasks/legal_retention.py``) deletes the row after that.

The rule lives in the DATABASE (trigger ``trg_legal_acceptance_guard``) so every deletion path,
including ones that bypass the services, behaves the same. The same trigger makes the table
append-only: the accepted text, its hash and time, scope and organization snapshot cannot be
rewritten, and a row can be deleted only by the retention sweep (GUC
``opentranscribe.legal_retention_sweep = 'on'``) once ``retain_until`` has passed. In a
single-role deployment that is an accident guard, not a security boundary.

``organization_id`` may only go to NULL (the organization FK's ``SET NULL``);
``organization_uuid`` still names the organization afterwards. A BEFORE INSERT trigger fills
``subject_user_uuid`` from the account, so any writer, old or new, records it.

Safe on a populated table: every statement is guarded and idempotent; the backfill touches
existing rows only; ``DROP NOT NULL`` and ``ADD COLUMN`` without a default do not rewrite; the
FK is re-added ``NOT VALID`` then validated. The FK is found by its columns, not by its name.

Downgrade is NOT lossless: it DELETES every pseudonymised row (``user_id IS NULL``, which the
restored ``NOT NULL`` cannot hold), then restores ``ON DELETE CASCADE``. Rollback by image to
v0.6.1 needs no downgrade: that code ignores the new columns, finds acceptances by ``user_id``
(so never sees pseudonymised rows) and lists them through an outer join.

Revision ID: v451_legal_acceptance_survives_user_deletion
Revises: v450_legal_acceptance_context_and_profile_interaction
Create Date: 2026-11-01
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v451_legal_acceptance_survives_user_deletion"
down_revision = "v450_legal_acceptance_context_and_profile_interaction"
branch_labels = None
depends_on = None

#: Frozen copies of ``core/constants.py`` values at the time of writing; the consistency test
#: asserts they still agree.
DEFAULT_RETENTION_DAYS = 2190
MIN_RETENTION_DAYS = 365

#: Module-level so the consistency test replays the real statements.
UPGRADE_SQL = f"""
    ALTER TABLE legal_acceptance ADD COLUMN IF NOT EXISTS subject_user_uuid UUID;
    ALTER TABLE legal_acceptance ADD COLUMN IF NOT EXISTS subject_erased_at TIMESTAMPTZ;
    ALTER TABLE legal_acceptance ADD COLUMN IF NOT EXISTS retain_until TIMESTAMPTZ;
    CREATE INDEX IF NOT EXISTS ix_legal_acceptance_retain_until
        ON legal_acceptance (retain_until) WHERE retain_until IS NOT NULL;
    CREATE INDEX IF NOT EXISTS ix_legal_acceptance_subject_user_uuid
        ON legal_acceptance (subject_user_uuid);

    UPDATE legal_acceptance la SET subject_user_uuid = u.uuid FROM "user" u
     WHERE la.user_id = u.id AND la.subject_user_uuid IS NULL;
    ALTER TABLE legal_acceptance ALTER COLUMN user_id DROP NOT NULL;

    DO $$
    DECLARE fk_name text;
    BEGIN
      SELECT c.conname INTO fk_name
        FROM pg_constraint c
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
       WHERE c.contype = 'f'
         AND c.conrelid = 'legal_acceptance'::regclass
         AND c.confrelid = '"user"'::regclass
         AND array_length(c.conkey, 1) = 1
         AND a.attname = 'user_id'
         AND c.confdeltype <> 'n';
      IF fk_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE legal_acceptance DROP CONSTRAINT %I', fk_name);
      END IF;
      IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
         WHERE c.contype = 'f' AND c.conrelid = 'legal_acceptance'::regclass
           AND c.confrelid = '"user"'::regclass AND c.confdeltype = 'n'
      ) THEN
        ALTER TABLE legal_acceptance ADD CONSTRAINT legal_acceptance_user_id_fkey
          FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE SET NULL NOT VALID;
        ALTER TABLE legal_acceptance VALIDATE CONSTRAINT legal_acceptance_user_id_fkey;
      END IF;
    END $$;

    CREATE OR REPLACE FUNCTION legal_acceptance_retention_days() RETURNS integer
    LANGUAGE plpgsql STABLE AS $fn$
    DECLARE raw text;
    BEGIN
      SELECT value INTO raw FROM system_settings
       WHERE key = 'legal.acceptance_retention_days';
      IF raw IS NOT NULL AND btrim(raw) ~ '^[0-9]{{1,6}}$' THEN
        RETURN GREATEST({MIN_RETENTION_DAYS}, btrim(raw)::integer);
      END IF;
      RETURN {DEFAULT_RETENTION_DAYS};
    EXCEPTION WHEN OTHERS THEN
      -- A settings read must never make a user deletion fail.
      RETURN {DEFAULT_RETENTION_DAYS};
    END $fn$;

    CREATE OR REPLACE FUNCTION legal_acceptance_guard() RETURNS trigger
    LANGUAGE plpgsql AS $fn$
    BEGIN
      IF TG_OP = 'DELETE' THEN
        IF current_setting('opentranscribe.legal_retention_sweep', true) = 'on'
           AND OLD.retain_until IS NOT NULL AND OLD.retain_until <= now() THEN
          RETURN OLD;
        END IF;
        RAISE EXCEPTION 'legal_acceptance rows are evidence: only the retention sweep may '
          'delete one, after retain_until' USING ERRCODE = 'restrict_violation';
      END IF;

      IF NEW.uuid IS DISTINCT FROM OLD.uuid
         OR NEW.document_key IS DISTINCT FROM OLD.document_key
         OR NEW.version IS DISTINCT FROM OLD.version
         OR NEW.sha256 IS DISTINCT FROM OLD.sha256
         OR NEW.accepted_at IS DISTINCT FROM OLD.accepted_at
         OR NEW.scope IS DISTINCT FROM OLD.scope
         OR NEW.locale IS DISTINCT FROM OLD.locale
         OR NEW.organization_uuid IS DISTINCT FROM OLD.organization_uuid
         OR (OLD.subject_user_uuid IS NOT NULL
             AND NEW.subject_user_uuid IS DISTINCT FROM OLD.subject_user_uuid)
         OR (NEW.organization_id IS DISTINCT FROM OLD.organization_id
             AND NEW.organization_id IS NOT NULL) THEN
        RAISE EXCEPTION 'legal_acceptance is append-only' USING ERRCODE = 'restrict_violation';
      END IF;

      IF OLD.user_id IS NOT NULL AND NEW.user_id IS NULL THEN
        -- The account is being deleted (FK SET NULL): pseudonymise and start the clock.
        NEW.ip_address := NULL;
        NEW.user_agent := NULL;
        NEW.subject_erased_at := now();
        NEW.retain_until := now() + make_interval(days => legal_acceptance_retention_days());
        RETURN NEW;
      END IF;

      IF NEW.user_id IS DISTINCT FROM OLD.user_id
         OR NEW.ip_address IS DISTINCT FROM OLD.ip_address
         OR NEW.user_agent IS DISTINCT FROM OLD.user_agent
         OR NEW.subject_erased_at IS DISTINCT FROM OLD.subject_erased_at
         OR NEW.retain_until IS DISTINCT FROM OLD.retain_until THEN
        RAISE EXCEPTION 'legal_acceptance is append-only' USING ERRCODE = 'restrict_violation';
      END IF;
      RETURN NEW;
    END $fn$;

    CREATE OR REPLACE FUNCTION legal_acceptance_stamp_subject() RETURNS trigger
    LANGUAGE plpgsql AS $fn$
    BEGIN
      IF NEW.subject_user_uuid IS NULL AND NEW.user_id IS NOT NULL THEN
        SELECT uuid INTO NEW.subject_user_uuid FROM "user" WHERE id = NEW.user_id;
      END IF;
      RETURN NEW;
    END $fn$;

    DROP TRIGGER IF EXISTS trg_legal_acceptance_stamp_subject ON legal_acceptance;
    CREATE TRIGGER trg_legal_acceptance_stamp_subject BEFORE INSERT ON legal_acceptance
      FOR EACH ROW EXECUTE FUNCTION legal_acceptance_stamp_subject();

    DO $$ BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_legal_acceptance_subject') THEN
        ALTER TABLE legal_acceptance ADD CONSTRAINT ck_legal_acceptance_subject CHECK (
          (user_id IS NOT NULL AND subject_erased_at IS NULL)
          OR (user_id IS NULL AND subject_erased_at IS NOT NULL AND retain_until IS NOT NULL
              AND ip_address IS NULL AND user_agent IS NULL));
      END IF;
    END $$;

    -- Created last: the detection arm keys on it (with the FK rule and the CHECK).
    DROP TRIGGER IF EXISTS trg_legal_acceptance_guard ON legal_acceptance;
    CREATE TRIGGER trg_legal_acceptance_guard BEFORE UPDATE OR DELETE ON legal_acceptance
      FOR EACH ROW EXECUTE FUNCTION legal_acceptance_guard();
"""

DOWNGRADE_SQL = """
    DROP TRIGGER IF EXISTS trg_legal_acceptance_guard ON legal_acceptance;
    DROP TRIGGER IF EXISTS trg_legal_acceptance_stamp_subject ON legal_acceptance;
    DROP FUNCTION IF EXISTS legal_acceptance_guard();
    DROP FUNCTION IF EXISTS legal_acceptance_stamp_subject();
    DROP FUNCTION IF EXISTS legal_acceptance_retention_days();
    ALTER TABLE legal_acceptance DROP CONSTRAINT IF EXISTS ck_legal_acceptance_subject;
    -- Irreversible: a pseudonymised row has no user to point at under NOT NULL.
    DELETE FROM legal_acceptance WHERE user_id IS NULL;
    DO $$
    DECLARE fk_name text;
    BEGIN
      SELECT c.conname INTO fk_name FROM pg_constraint c
       WHERE c.contype = 'f' AND c.conrelid = 'legal_acceptance'::regclass
         AND c.confrelid = '"user"'::regclass AND c.confdeltype <> 'c';
      IF fk_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE legal_acceptance DROP CONSTRAINT %I', fk_name);
        ALTER TABLE legal_acceptance ADD CONSTRAINT legal_acceptance_user_id_fkey
          FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE;
      END IF;
    END $$;
    ALTER TABLE legal_acceptance ALTER COLUMN user_id SET NOT NULL;
    DROP INDEX IF EXISTS ix_legal_acceptance_subject_user_uuid;
    DROP INDEX IF EXISTS ix_legal_acceptance_retain_until;
    ALTER TABLE legal_acceptance DROP COLUMN IF EXISTS retain_until;
    ALTER TABLE legal_acceptance DROP COLUMN IF EXISTS subject_erased_at;
    ALTER TABLE legal_acceptance DROP COLUMN IF EXISTS subject_user_uuid;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
