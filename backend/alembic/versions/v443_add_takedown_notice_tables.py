"""Record takedown notices and the files they cover (issue #1254).

Why
---
Quarantining a file was a button with a reason field. The reason lives on the
``media_file`` row and is wiped on release, and the audit trail is a log with a
retention setting, so an operator could not answer "which notice did we receive, about
which file, and what became of it" once the file was purged or the year turned.

What
----
* ``takedown_notice``: one row per legal notice (or per operator action that had none):
  type, received-at, an opaque requester reference, the internal basis, who handled it.
* ``takedown_notice_file``: one row per file a notice covers, with that file's outcome
  (``open`` / ``counter_notice`` / ``upheld`` / ``released``). The file, its owner and its
  organization are snapshot uuids beside ``ON DELETE SET NULL`` keys, so the record
  survives a purge, an account deletion and an organization erasure.
* Backfill: every file that is quarantined or under legal hold gets a synthetic
  ``operator_action`` notice and link, so "no quarantined file without a record" holds from
  the first day. A re-run is a no-op (guarded by ``NOT EXISTS``).

The tables hold no requester name, address or other contact detail on purpose.

Downgrade DROPS the tables and with them the compliance evidence they hold. It exists for
the consistency test and for a developer rolling back before any notice was recorded;
never run it against a deployment that has recorded real notices.

Revision ID: v443_add_takedown_notice_tables
Revises: v442_add_media_file_takedown_origin
Create Date: 2026-10-21
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v443_add_takedown_notice_tables"
down_revision = "v442_add_media_file_takedown_origin"
branch_labels = None
depends_on = None

#: Backfill text for a held file whose live reason was already wiped by a release.
BACKFILL_REASON = "Backfilled: held before notice records existed"

#: Module-level so the consistency test replays the real statements.
CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS takedown_notice (
        id SERIAL PRIMARY KEY,
        uuid UUID NOT NULL,
        notice_type VARCHAR(24) NOT NULL,
        received_at TIMESTAMPTZ NOT NULL,
        requester_reference VARCHAR(255),
        basis VARCHAR(2000) NOT NULL,
        recorded_by_authority VARCHAR(16) NOT NULL DEFAULT 'platform',
        handled_by_user_id INTEGER REFERENCES "user"(id) ON DELETE SET NULL,
        handled_by_user_uuid UUID,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE UNIQUE INDEX IF NOT EXISTS ix_takedown_notice_uuid ON takedown_notice (uuid);
    CREATE INDEX IF NOT EXISTS ix_takedown_notice_received_at ON takedown_notice (received_at);

    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_takedown_notice_type_valid') THEN
            ALTER TABLE takedown_notice ADD CONSTRAINT ck_takedown_notice_type_valid
                CHECK (notice_type IN ('dmca', 'court_order', 'abuse_report', 'aup', 'other',
                                       'operator_action'));
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_takedown_notice_authority_valid') THEN
            ALTER TABLE takedown_notice ADD CONSTRAINT ck_takedown_notice_authority_valid
                CHECK (recorded_by_authority IN ('platform', 'organization'));
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
             WHERE conname = 'ck_takedown_notice_operator_action_has_no_requester'
        ) THEN
            ALTER TABLE takedown_notice
                ADD CONSTRAINT ck_takedown_notice_operator_action_has_no_requester
                CHECK ((notice_type = 'operator_action') = (requester_reference IS NULL));
        END IF;
    END $$;

    CREATE TABLE IF NOT EXISTS takedown_notice_file (
        id SERIAL PRIMARY KEY,
        uuid UUID NOT NULL,
        notice_id INTEGER NOT NULL REFERENCES takedown_notice(id) ON DELETE CASCADE,
        file_id INTEGER REFERENCES media_file(id) ON DELETE SET NULL,
        file_uuid UUID NOT NULL,
        owner_user_uuid UUID NOT NULL,
        organization_id INTEGER REFERENCES organization(id) ON DELETE SET NULL,
        organization_uuid UUID,
        owner_reason VARCHAR(2000) NOT NULL,
        status VARCHAR(16) NOT NULL DEFAULT 'open',
        quarantined_at TIMESTAMPTZ NOT NULL,
        counter_notice_received_at TIMESTAMPTZ,
        outcome_at TIMESTAMPTZ,
        outcome_note VARCHAR(2000),
        legal_hold_placed BOOLEAN NOT NULL DEFAULT false
    );
    CREATE UNIQUE INDEX IF NOT EXISTS ix_takedown_notice_file_uuid ON takedown_notice_file (uuid);
    CREATE INDEX IF NOT EXISTS ix_takedown_notice_file_notice_id
        ON takedown_notice_file (notice_id);
    CREATE INDEX IF NOT EXISTS ix_takedown_notice_file_live ON takedown_notice_file (file_id)
        WHERE status IN ('open', 'counter_notice', 'upheld');
    CREATE INDEX IF NOT EXISTS ix_takedown_notice_file_org_quarantined
        ON takedown_notice_file (organization_id, quarantined_at);
    CREATE INDEX IF NOT EXISTS ix_takedown_notice_file_owner
        ON takedown_notice_file (owner_user_uuid);

    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'uq_takedown_notice_file_notice_file'
        ) THEN
            ALTER TABLE takedown_notice_file ADD CONSTRAINT uq_takedown_notice_file_notice_file
                UNIQUE (notice_id, file_uuid);
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'ck_takedown_notice_file_status_valid'
        ) THEN
            ALTER TABLE takedown_notice_file ADD CONSTRAINT ck_takedown_notice_file_status_valid
                CHECK (status IN ('open', 'counter_notice', 'upheld', 'released'));
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'ck_takedown_notice_file_counter_notice_at'
        ) THEN
            ALTER TABLE takedown_notice_file
                ADD CONSTRAINT ck_takedown_notice_file_counter_notice_at
                CHECK (status <> 'counter_notice' OR counter_notice_received_at IS NOT NULL);
        END IF;
        -- Created last: the detection arm keys on it (a table without its CHECKs is the
        -- state that must re-run this revision).
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'ck_takedown_notice_file_outcome_at'
        ) THEN
            ALTER TABLE takedown_notice_file ADD CONSTRAINT ck_takedown_notice_file_outcome_at
                CHECK (status NOT IN ('upheld', 'released') OR outcome_at IS NOT NULL);
        END IF;
    END $$;
"""

#: One synthetic ``operator_action`` notice plus one link per held file that has none.
#: ``%(reason)s`` is bound by the caller, so the text lives in one place.
BACKFILL_SQL = """
    WITH src AS (
        SELECT mf.id AS file_id,
               mf.uuid AS file_uuid,
               u.uuid AS owner_uuid,
               mf.organization_id,
               org.uuid AS organization_uuid,
               mf.is_quarantined,
               mf.legal_hold,
               mf.quarantined_by,
               hu.uuid AS handler_uuid,
               COALESCE(mf.quarantined_at, now()) AS received_at,
               LEFT(COALESCE(NULLIF(mf.quarantine_reason, ''), :reason), 2000) AS reason,
               CASE WHEN mf.takedown_origin = 'organization' THEN 'organization'
                    ELSE 'platform' END AS authority,
               gen_random_uuid() AS notice_uuid
          FROM media_file mf
          JOIN "user" u ON u.id = mf.user_id
          LEFT JOIN organization org ON org.id = mf.organization_id
          LEFT JOIN "user" hu ON hu.id = mf.quarantined_by
         WHERE (mf.is_quarantined OR mf.legal_hold)
           AND NOT EXISTS (
               SELECT 1 FROM takedown_notice_file l WHERE l.file_uuid = mf.uuid
           )
    ),
    new_notice AS (
        INSERT INTO takedown_notice (
            uuid, notice_type, received_at, requester_reference, basis,
            recorded_by_authority, handled_by_user_id, handled_by_user_uuid
        )
        SELECT notice_uuid, 'operator_action', received_at, NULL, reason,
               authority, quarantined_by, handler_uuid
          FROM src
        RETURNING id, uuid
    )
    INSERT INTO takedown_notice_file (
        uuid, notice_id, file_id, file_uuid, owner_user_uuid, organization_id,
        organization_uuid, owner_reason, status, quarantined_at, outcome_at, outcome_note,
        legal_hold_placed
    )
    SELECT gen_random_uuid(), n.id, src.file_id, src.file_uuid, src.owner_uuid,
           src.organization_id, src.organization_uuid, src.reason,
           CASE WHEN src.is_quarantined THEN 'open' ELSE 'released' END,
           src.received_at,
           CASE WHEN src.is_quarantined THEN NULL ELSE now() END,
           CASE WHEN src.is_quarantined THEN NULL ELSE 'backfill' END,
           src.legal_hold
      FROM src
      JOIN new_notice n ON n.uuid = src.notice_uuid;
"""

DOWNGRADE_SQL = """
    DROP TABLE IF EXISTS takedown_notice_file;
    DROP TABLE IF EXISTS takedown_notice;
"""


def upgrade():
    op.execute(CREATE_SQL)
    from sqlalchemy import text

    op.get_bind().execute(text(BACKFILL_SQL), {"reason": BACKFILL_REASON})


def downgrade():
    op.execute(DOWNGRADE_SQL)
