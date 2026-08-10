"""Drop three tables left behind by removed features (issue #398).

``upload_session``, ``speaker_audio_clip`` and ``user_certificate_preferences``
exist in every deployment's database and are referenced by **nothing**: no
SQLAlchemy model, no query, no raw SQL, no script. They are the residue of
features that were removed without a corresponding drop, and they were invisible
until ``scripts/check-schema-drift.py`` started comparing ``Base.metadata``
against the live schema.

Evidence gathered before writing this (all three, on a long-lived dev database):

* **Zero rows.** Nothing has ever written to them, or whatever did was removed
  along with the writer.
* **Zero references** anywhere outside ``backend/alembic/versions`` — the greps
  hit only the drift tooling that reported them, two prose docs, and
  ``database/init_db.sql``.
* ``database/init_db.sql`` is the **legacy** v0.3.x bootstrap and is no longer
  mounted by any compose file (``docker-compose.nas.yml`` carries an explicit
  "Do NOT mount init_db.sql here" comment). Alembic owns the schema now.

Where they came from:
  ``upload_session``               — v280_add_upload_sessions
  ``speaker_audio_clip``           — v220_add_speaker_clusters
  ``user_certificate_preferences`` — v080_add_auth_config

Dropping them is safe *because* they are empty and unreferenced, but the
downgrade cannot honestly restore them: recreating an empty table with a guessed
column list would be worse than not recreating it, since it would claim a
fidelity it does not have. The downgrade is therefore deliberately a no-op, and
says so — see ``downgrade()``.

Uses ``IF EXISTS`` / ``CASCADE`` so it is idempotent and safe to re-run against a
partially-migrated database, per backend/alembic/CLAUDE.md.

Revision ID: v385_drop_orphan_tables
Revises: v384_add_chat_reasoning_content
Create Date: 2026-08-10
"""

from alembic import op

revision = "v385_drop_orphan_tables"
down_revision = "v384_add_chat_reasoning_content"
branch_labels = None
depends_on = None


# Named here rather than inline so the detection arm in app/db/migrations.py and
# the consistency test can refer to the same list.
ORPHAN_TABLES = (
    "upload_session",
    "speaker_audio_clip",
    "user_certificate_preferences",
)


def upgrade():
    for table in ORPHAN_TABLES:
        # CASCADE removes the table's own indexes and any FK constraints that
        # point AT it. Each is unreferenced, so in practice this drops only the
        # table's own dependent objects — but a deployment that somehow grew a
        # view over one of them should not wedge the migration chain.
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")


def downgrade():
    """Intentionally a no-op.

    These tables were empty and unreferenced everywhere. Recreating them would
    mean inventing a column list from a migration that no longer describes any
    running code, producing an empty table that looks restored but is not — and
    nothing would read it either way. Leaving them absent is the honest inverse
    of dropping something dead.
    """
