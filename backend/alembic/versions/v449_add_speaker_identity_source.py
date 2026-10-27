"""Record whether a speaker's name and profile link came from a person or a voiceprint (#1280).

Why
---
Switching voiceprints off must hide what a voiceprint produced, and a voiceprint purge must
delete it, while a name a person typed or confirmed is never touched. The commonest voice
identification could not be told apart: when a voiceprint match is confident
(``SPEAKER_CONFIDENCE_HIGH``, 0.75) the matcher writes ``display_name``, ``verified = TRUE``
and ``profile_id`` -- exactly what a person's confirmation writes. A person accepting a voice
suggestion in the Inbox leaves a byte-identical row. ``suggestion_source`` describes
``suggested_name`` and is read standalone elsewhere, so it cannot carry this.

What
----
* ``speaker.identity_source VARCHAR(16)``, nullable, no default (metadata-only: no table
  rewrite). Values ``voiceprint`` (the machine wrote name and link), ``voiceprint_link`` (it
  added only the link to an existing name), ``human`` (a person did), NULL (unknown: written
  before this revision, or never named; every reader treats it as ``human``). Writers go
  through ``app/utils/speaker_identity_source.py``.
* ``ck_speaker_identity_source`` admits exactly those values. Added validated in the same
  transaction: the column is new and NULL everywhere, so the check scan is a read of NULLs
  under the lock the ``ADD COLUMN`` already holds; a ``NOT VALID`` + ``VALIDATE`` pair buys
  nothing inside one migration transaction.
* No index: the purge and the label resync filter by scope first.

Backfill (owner decision Q1, 2026-10-27)
----------------------------------------
Existing rows are unknown, i.e. a person's, except one shape that only the voiceprint
matcher produces. The only live writer of ``suggestion_source = 'profile_match'`` is
``speaker_matching_service._handle_speaker_match``, and it stores that suggestion only at
confidence >= 0.75, the same comparison that decides ``auto_accept``; so every such row was
auto-accepted when written. A later human rename clears ``suggested_name``, so a row still
showing the shape has not been renamed since. Stamped ``voiceprint`` only where ALL hold:
the row is still unmarked, ``suggestion_source = 'profile_match'``, ``verified IS TRUE``, a
profile is linked, the suggestion is non-empty, ``display_name`` equals it, and the linked
profile carries that name. Confidence is deliberately not in the predicate: a re-match can
lower it without touching the suggestion.

Residual false positives (disclosed in the upgrade note): rows ``v439`` stamped from data
written before ``v091`` where a person had linked the same-named profile, and a person who
later re-assigned the identical profile in a batch. Under-recognised (left NULL, so treated
as a person's, never hidden or purged): ``voice_match`` auto-accepts (identical to an Inbox
accept), propagation links, retroactive matches and upload cluster joins. Postgres cannot
tell those apart.

The number of rows stamped is logged and recorded in ``system_settings`` under
``privacy.identity_source_backfill`` (JSON ``{"revision", "stamped", "recorded_at"}``) so
the upgrade note can point operators at it. The record is written once (``ON CONFLICT DO
NOTHING``): a re-run stamps nothing and keeps the first count. Downgrade removes it.

Downgrade drops the constraint, the column and the record. Names and links are untouched;
only the markers are lost (a purge already run is irreversible either way).

Community edition: same behaviour; nothing here is edition-specific.

Integration note: this revision sits on ``v447_add_content_hold`` because the branch it was
written on does not hold ``v448_task_one_active_row_per_file_step`` (also a child of v447).
When both reach one branch, set ``down_revision = "v448_task_one_active_row_per_file_step"``
here and in ``tests/unit/test_v449_migration_consistency.py`` (``PARENT``);
``test_v449_revision_chain`` fails with that instruction until it is done.

Revision ID: v449_add_speaker_identity_source
Revises: v447_add_content_hold
Create Date: 2026-10-27
"""

import json
import logging

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "v449_add_speaker_identity_source"
down_revision = "v447_add_content_hold"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

#: ``system_settings`` key of the backfill record. Mirrors
#: ``app.utils.speaker_identity_source.BACKFILL_RECORD_SETTING_KEY`` (pinned equal by the
#: consistency test; a migration does not import application code).
BACKFILL_RECORD_KEY = "privacy.identity_source_backfill"

ADD_COLUMN_SQL = """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_name = 'speaker' AND column_name = 'identity_source'
        ) THEN
            ALTER TABLE speaker ADD COLUMN identity_source VARCHAR(16);
        END IF;
    END $$;
"""

#: Created after the column; the detection arm keys on it.
ADD_CHECK_SQL = """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'ck_speaker_identity_source'
        ) THEN
            ALTER TABLE speaker ADD CONSTRAINT ck_speaker_identity_source
                CHECK (identity_source IS NULL
                       OR identity_source IN ('human', 'voiceprint', 'voiceprint_link'));
        END IF;
    END $$;
"""

#: The one pre-upgrade shape provably written by the voiceprint auto-accept. ``s`` is
#: ``speaker``.
BACKFILL_WHERE = """
    s.identity_source IS NULL
    AND s.suggestion_source = 'profile_match'
    AND s.verified IS TRUE
    AND s.profile_id IS NOT NULL
    AND coalesce(s.suggested_name, '') <> ''
    AND s.display_name = s.suggested_name
    AND EXISTS (
        SELECT 1 FROM speaker_profile p WHERE p.id = s.profile_id AND p.name = s.display_name
    )
"""

BACKFILL_SQL = f"UPDATE speaker s SET identity_source = 'voiceprint' WHERE {BACKFILL_WHERE}"

RECORD_SQL = """
    INSERT INTO system_settings (key, value, description)
    VALUES (:key, :value, :description)
    ON CONFLICT (key) DO NOTHING
"""

RECORD_DESCRIPTION = (
    "Rows the v449 upgrade recognised as named by a voiceprint auto-accept (issue #1280). "
    "Written once by the migration; informational."
)

DOWNGRADE_SQL = """
    ALTER TABLE speaker DROP CONSTRAINT IF EXISTS ck_speaker_identity_source;
    ALTER TABLE speaker DROP COLUMN IF EXISTS identity_source;
"""

DELETE_RECORD_SQL = "DELETE FROM system_settings WHERE key = :key"


def backfill_and_record(conn) -> int:
    """Stamp the provable shape, record and log the count. Returns the rows stamped.

    Module-level so the consistency test runs exactly what the upgrade runs.
    """
    stamped = int(conn.execute(sa.text(BACKFILL_SQL)).rowcount or 0)
    recorded_at = conn.execute(sa.text("SELECT now()")).scalar()
    conn.execute(
        sa.text(RECORD_SQL),
        {
            "key": BACKFILL_RECORD_KEY,
            "value": json.dumps(
                {"revision": revision, "stamped": stamped, "recorded_at": str(recorded_at)}
            ),
            "description": RECORD_DESCRIPTION,
        },
    )
    logger.info(
        "v449: recognised %d existing speaker identit%s as voiceprint-made "
        "(recorded in system_settings %r)",
        stamped,
        "y" if stamped == 1 else "ies",
        BACKFILL_RECORD_KEY,
    )
    return stamped


def upgrade():
    op.execute(ADD_COLUMN_SQL)
    op.execute(ADD_CHECK_SQL)
    backfill_and_record(op.get_bind())


def downgrade():
    op.execute(DOWNGRADE_SQL)
    op.get_bind().execute(sa.text(DELETE_RECORD_SQL), {"key": BACKFILL_RECORD_KEY})
