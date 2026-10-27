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
* ``SET LOCAL lock_timeout = '30s'`` first: ``ADD COLUMN`` and ``ADD CONSTRAINT`` take
  ``ACCESS EXCLUSIVE`` on ``speaker``, and an unbounded wait would queue every reader behind a
  long-running transaction. A busy table fails the upgrade loudly instead; re-run it.

Backfill (owner decision Q1, 2026-10-27; narrowed after audit)
--------------------------------------------------------------
Existing rows are unknown, i.e. a person's, unless there is POSITIVE evidence that a voiceprint
wrote them. Mistaking a person's name for a machine's is data loss (a later purge clears it and
nothing restores it); mistaking a machine's for a person's only leaves a name visible, so the
backfill errs toward NULL.

The one shape the voiceprint matcher produces: ``speaker_matching_service._handle_speaker_match``
stores a ``profile_match`` suggestion only at confidence >= 0.75, the comparison that decides
``auto_accept``, and then writes ``display_name``, ``verified`` and ``profile_id``. But before
v449 that function also RE-MATCHED a speaker that already had a name (a re-extracted voiceprint),
rewriting the same fields over a name a person had typed -- and a person typing a new name creates
a profile named for it and links the speaker. That row is byte-identical to an auto-accept, so
the shape alone is not evidence. A shape row is stamped ``voiceprint`` only when ALL hold:

* the shape: still unmarked, ``suggestion_source = 'profile_match'``, ``verified IS TRUE``, a
  profile linked, a non-empty suggestion, ``display_name`` equal to it, and the linked profile
  carrying that name (confidence is deliberately not in the predicate: a re-match can lower it
  without touching the suggestion); AND
* the profile already existed when the speaker was created (``speaker.created_at`` after
  ``speaker_profile.created_at``). A person who names a speaker creates the profile AFTER the
  speaker exists, so the speaker that originated a profile never qualifies; AND
* another speaker linked to that profile was created before this one. A match compares against
  the profile's embedding, which only exists once a speaker has been linked to it.

Left NULL (read as a person's, never hidden, never purged): every shape row failing either
condition; ``voice_match`` auto-accepts (identical to an Inbox accept); propagation links and
retroactive matches onto speakers older than their profile; upload cluster joins.

Residual, disclosed in the upgrade note: a person who assigned an EXISTING profile to a later
speaker and whose voiceprint was then re-extracted still has the stamped shape, and so does a
batch assignment of the same profile that a person made. A later purge would clear those names.
The count of rows that had the shape but were NOT stamped is recorded next to the stamped count,
so an operator can see how many ambiguous rows were left alone.

The number of rows stamped, and of shape rows left unmarked, is logged and recorded in
``system_settings`` under ``privacy.identity_source_backfill`` (JSON ``{"revision", "stamped",
"shape_left_unmarked", "recorded_at"}``) so the upgrade note can point operators at it
(``SELECT value FROM system_settings WHERE key = 'privacy.identity_source_backfill'``). The record is written once (``ON CONFLICT DO
NOTHING``): a re-run stamps nothing and keeps the first count. Downgrade removes it.

Downgrade drops the constraint, the column and the record. Names and links are untouched;
only the markers are lost (a purge already run is irreversible either way).

Community edition: same behaviour; nothing here is edition-specific.

Revision ID: v449_add_speaker_identity_source
Revises: v448_task_one_active_row_per_file_step
Create Date: 2026-10-27
"""

import json
import logging

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "v449_add_speaker_identity_source"
down_revision = "v448_task_one_active_row_per_file_step"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

#: ``system_settings`` key of the backfill record. Mirrors
#: ``app.utils.speaker_identity_source.BACKFILL_RECORD_SETTING_KEY`` (pinned equal by the
#: consistency test; a migration does not import application code).
BACKFILL_RECORD_KEY = "privacy.identity_source_backfill"

#: Bounds every lock wait of this revision's transaction (see the docstring).
LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = '30s'"

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

#: The pre-upgrade shape a voiceprint auto-accept writes. ``s`` is ``speaker``. A person's
#: name that a later re-match rewrote has the same shape; :data:`EVIDENCE_WHERE` tells them apart.
SHAPE_WHERE = """
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

#: Positive evidence that the PROFILE came first and that a voice was already linked to it, which
#: an automatic match needs and a person naming a speaker (who creates the profile after the
#: speaker) does not provide. NULL timestamps prove nothing and fail it.
EVIDENCE_WHERE = """
    EXISTS (
        SELECT 1 FROM speaker_profile p
         WHERE p.id = s.profile_id
           AND s.created_at > p.created_at
           AND EXISTS (
               SELECT 1 FROM speaker o
                WHERE o.profile_id = p.id AND o.id <> s.id AND o.created_at < s.created_at
           )
    )
"""

BACKFILL_WHERE = f"{SHAPE_WHERE} AND {EVIDENCE_WHERE}"

SHAPE_COUNT_SQL = f"SELECT count(*) FROM speaker s WHERE {SHAPE_WHERE}"

BACKFILL_SQL = f"UPDATE speaker s SET identity_source = 'voiceprint' WHERE {BACKFILL_WHERE}"

RECORD_SQL = """
    INSERT INTO system_settings (key, value, description)
    VALUES (:key, :value, :description)
    ON CONFLICT (key) DO NOTHING
"""

RECORD_DESCRIPTION = (
    "Rows the v449 upgrade recognised as named by a voiceprint auto-accept, and rows with that "
    "shape it left unmarked for lack of evidence (issue #1280). Written once; informational."
)

DOWNGRADE_SQL = """
    ALTER TABLE speaker DROP CONSTRAINT IF EXISTS ck_speaker_identity_source;
    ALTER TABLE speaker DROP COLUMN IF EXISTS identity_source;
"""

DELETE_RECORD_SQL = "DELETE FROM system_settings WHERE key = :key"


def backfill_and_record(conn) -> int:
    """Stamp the evidenced shape, record and log the counts. Returns the rows stamped.

    Module-level so the consistency test runs exactly what the upgrade runs.
    """
    shape_rows = int(conn.execute(sa.text(SHAPE_COUNT_SQL)).scalar() or 0)
    stamped = int(conn.execute(sa.text(BACKFILL_SQL)).rowcount or 0)
    left_unmarked = max(shape_rows - stamped, 0)
    recorded_at = conn.execute(sa.text("SELECT now()")).scalar()
    conn.execute(
        sa.text(RECORD_SQL),
        {
            "key": BACKFILL_RECORD_KEY,
            "value": json.dumps(
                {
                    "revision": revision,
                    "stamped": stamped,
                    "shape_left_unmarked": left_unmarked,
                    "recorded_at": str(recorded_at),
                }
            ),
            "description": RECORD_DESCRIPTION,
        },
    )
    logger.info(
        "v449: recognised %d existing speaker identit%s as voiceprint-made and left %d "
        "ambiguous one%s unmarked (recorded in system_settings %r)",
        stamped,
        "y" if stamped == 1 else "ies",
        left_unmarked,
        "" if left_unmarked == 1 else "s",
        BACKFILL_RECORD_KEY,
    )
    return stamped


def upgrade():
    op.execute(LOCK_TIMEOUT_SQL)
    op.execute(ADD_COLUMN_SQL)
    op.execute(ADD_CHECK_SQL)
    backfill_and_record(op.get_bind())


def downgrade():
    op.execute(DOWNGRADE_SQL)
    op.get_bind().execute(sa.text(DELETE_RECORD_SQL), {"key": BACKFILL_RECORD_KEY})
