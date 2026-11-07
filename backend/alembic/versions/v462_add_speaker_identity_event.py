"""Append-only history of every change to a speaker's name, profile link and suggestion (#1336).

Why
---
``speaker.identity_source`` and its record columns (v449, issue #1280) describe only the CURRENT
name, link and recommendation: who made it, when, and from what. Each new write overwrites the
last, so nothing can show a user how a name came to be, support an audit, or say which model
made a mistaken machine match so it can be undone. ``speaker_identity_event`` keeps one row per
change, written in the SAME transaction as the change (``app/services/speaker_identity_history``).

- ``speaker_id`` ``ON DELETE CASCADE``: the history is part of the speaker and goes with it (and
  so with its file). ``organization_id`` is the tenant stamp copied from the speaker,
  ``ON DELETE CASCADE`` like the speaker's file.
- ``actor_kind`` ``user`` | ``system``; ``actor_user_id`` ``ON DELETE SET NULL``: erasing a
  person de-attributes what they did on files that are not theirs (``ck_..._actor``: a system
  row never names a person).
- ``kind``: closed set (``ck_..._kind``). ``origin``, ``model``, ``confidence`` and ``detail``
  come from the origin records (``app/utils/speaker_identity_detail``: ids only, never a name or
  transcript text). ``before_value`` / ``after_value`` hold only the fields that changed.
- ``voiceprint``: the row records something a voiceprint produced. A voiceprint purge deletes
  those rows; while a file's voiceprints are off they are withheld from readers. A value a
  voiceprint wrote is never copied into any other row.

Append-only (layer 1, the same as v453's audit tables; layer 2 is the optional app role, whose
grants come from ``app.db.audit_protection``): ``speaker_identity_event_guard()`` refuses every
UPDATE except ``actor_user_id`` going to NULL (the user deletion's ``SET NULL``), refuses
TRUNCATE, and refuses DELETE unless it is the ``ON DELETE CASCADE`` of the speaker or
organization (``pg_trigger_depth() > 1``: the cascade deletes from inside its own trigger) or it
removes a ``voiceprint`` row while ``opentranscribe.voiceprint_purge = 'on'`` (set LOCAL by the
purge, the switch shape of v451's ``opentranscribe.legal_retention_sweep``). The table owner
passes with ``opentranscribe.audit_maintenance = 'on'`` (v453 ``audit_maintenance_allowed``).

Every deployment gets the table, empty until a speaker is named. The detection arm keys on the
guard trigger, created last. The downgrade drops the table and the history with it.

Revision ID: v462_add_speaker_identity_event
Revises: v461_add_privacy_purge_pending
Create Date: 2026-11-07
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v462_add_speaker_identity_event"
down_revision = "v461_add_privacy_purge_pending"
branch_labels = None
depends_on = None

#: Frozen copy of ``app.models.speaker_identity_event.EVENT_KINDS`` at this revision, spelled out
#: in ``TABLE_SQL``'s CHECK; the consistency test asserts all three agree.
KINDS = (
    "rename",
    "name_cleared",
    "link",
    "unlink",
    "suggested",
    "suggestion_accepted",
    "suggestion_rejected",
    "suggestion_cleared",
    "confirmed",
    "skipped",
    "merge",
    "purge",
)

#: The session setting a voiceprint purge sets LOCAL to delete ``voiceprint`` rows (spelled out in
#: ``GUARD_SQL``; the consistency test asserts it).
PURGE_SWITCH = "opentranscribe.voiceprint_purge"

#: Module-level so a consistency test can replay the real statements.
TABLE_SQL = """
CREATE TABLE IF NOT EXISTS speaker_identity_event (
    id BIGSERIAL PRIMARY KEY,
    uuid UUID NOT NULL UNIQUE,
    speaker_id INTEGER NOT NULL REFERENCES speaker(id) ON DELETE CASCADE,
    organization_id INTEGER REFERENCES organization(id) ON DELETE CASCADE,
    kind VARCHAR(32) NOT NULL,
    actor_kind VARCHAR(16) NOT NULL,
    actor_user_id INTEGER REFERENCES "user"(id) ON DELETE SET NULL,
    origin VARCHAR(64),
    model VARCHAR(255),
    confidence DOUBLE PRECISION,
    voiceprint BOOLEAN NOT NULL DEFAULT false,
    before_value JSONB,
    after_value JSONB,
    detail JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_speaker_identity_event_speaker_created
    ON speaker_identity_event (speaker_id, created_at);
CREATE INDEX IF NOT EXISTS ix_speaker_identity_event_organization_id
    ON speaker_identity_event (organization_id);
CREATE INDEX IF NOT EXISTS ix_speaker_identity_event_actor_user_id
    ON speaker_identity_event (actor_user_id) WHERE actor_user_id IS NOT NULL;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_speaker_identity_event_kind')
    THEN
        ALTER TABLE speaker_identity_event ADD CONSTRAINT ck_speaker_identity_event_kind
            CHECK (kind IN ('rename', 'name_cleared', 'link', 'unlink', 'suggested',
                            'suggestion_accepted', 'suggestion_rejected', 'suggestion_cleared',
                            'confirmed', 'skipped', 'merge', 'purge'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_speaker_identity_event_actor')
    THEN
        ALTER TABLE speaker_identity_event ADD CONSTRAINT ck_speaker_identity_event_actor
            CHECK (actor_kind IN ('user', 'system')
                   AND (actor_kind = 'user' OR actor_user_id IS NULL));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_speaker_identity_event_json')
    THEN
        ALTER TABLE speaker_identity_event ADD CONSTRAINT ck_speaker_identity_event_json
            CHECK ((before_value IS NULL OR jsonb_typeof(before_value) = 'object')
                   AND (after_value IS NULL OR jsonb_typeof(after_value) = 'object')
                   AND (detail IS NULL OR jsonb_typeof(detail) = 'object'));
    END IF;
END $$;
"""

GUARD_SQL = """
CREATE OR REPLACE FUNCTION speaker_identity_event_guard() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
  IF audit_maintenance_allowed(TG_RELID) THEN
    IF TG_LEVEL = 'STATEMENT' THEN RETURN NULL; END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
  END IF;
  IF TG_OP = 'TRUNCATE' THEN
    RAISE EXCEPTION 'speaker identity history is append-only: TRUNCATE refused'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF TG_OP = 'DELETE' THEN
    -- The speaker or organization the row describes is being deleted: the FK cascade runs
    -- this DELETE from inside its own trigger.
    IF pg_trigger_depth() > 1 THEN
      RETURN OLD;
    END IF;
    IF OLD.voiceprint
       AND coalesce(current_setting('opentranscribe.voiceprint_purge', true), '') = 'on' THEN
      RETURN OLD;
    END IF;
    RAISE EXCEPTION 'speaker identity history is append-only: DELETE refused'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  -- UPDATE: only the user deletion's ON DELETE SET NULL of the actor.
  IF (to_jsonb(NEW) - 'actor_user_id') IS DISTINCT FROM (to_jsonb(OLD) - 'actor_user_id')
     OR (NEW.actor_user_id IS NOT NULL
         AND NEW.actor_user_id IS DISTINCT FROM OLD.actor_user_id) THEN
    RAISE EXCEPTION 'speaker identity history is append-only: UPDATE refused'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $fn$;

DROP TRIGGER IF EXISTS trg_audit_guard_truncate_speaker_identity_event ON speaker_identity_event;
CREATE TRIGGER trg_audit_guard_truncate_speaker_identity_event BEFORE TRUNCATE
  ON speaker_identity_event
  FOR EACH STATEMENT EXECUTE FUNCTION speaker_identity_event_guard();
DROP TRIGGER IF EXISTS trg_speaker_identity_event_guard ON speaker_identity_event;
CREATE TRIGGER trg_speaker_identity_event_guard BEFORE UPDATE OR DELETE
  ON speaker_identity_event
  FOR EACH ROW EXECUTE FUNCTION speaker_identity_event_guard();
"""


DOWNGRADE_SQL = (
    "DROP TABLE IF EXISTS speaker_identity_event;"
    "DROP FUNCTION IF EXISTS speaker_identity_event_guard();"
)


def upgrade() -> None:
    op.execute(TABLE_SQL)
    op.execute(GUARD_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
