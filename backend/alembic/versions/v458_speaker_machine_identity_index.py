"""Partial index for the voiceprint-identity privacy sweeps on ``speaker`` (issue #1328).

Why
---
Every privacy sweep (voiceprint purge, residual purge, label reindex, the redaction view) selects
speakers with ``identity_source IN ('voiceprint', 'voiceprint_link')`` (or ``= 'voiceprint'``)
together with ``media_file_id IN (<files in scope>)``. Those rows are a small fraction of
``speaker``, and ``identity_source`` has no index, so each sweep read the whole table.

``ix_speaker_machine_identity_media_file`` is a PARTIAL index on ``media_file_id`` restricted to
exactly those two values. The predicate is the filter every caller has (the planner proves
``identity_source = 'voiceprint'`` implies the ``IN`` list), and the key column is the one the
sweeps look rows up by, so the index stays small and serves both shapes.

The plain ``speaker.media_file_id`` index the issue also asked for already exists
(``idx_speaker_media_file_id``, ``v010`` and ``v120``); a second one would only slow writes, so none
is created.

The build is ``CREATE INDEX CONCURRENTLY`` inside Alembic's ``autocommit_block()``, so writers keep
working. A failed concurrent build leaves an INVALID index that ``IF NOT EXISTS`` would accept, so
an invalid leftover is dropped first. Idempotent. The downgrade drops the index and destroys no
data.

Revision ID: v458_speaker_machine_identity_index
Revises: v457_audit_guard_followups
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v458_speaker_machine_identity_index"
down_revision = "v457_audit_guard_followups"
branch_labels = None
depends_on = None

DROP_INVALID_SQL = """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
        WHERE c.relname = 'ix_speaker_machine_identity_media_file' AND NOT i.indisvalid
    ) THEN
        EXECUTE 'DROP INDEX IF EXISTS ix_speaker_machine_identity_media_file';
    END IF;
END $$;
"""

CREATE_SQL = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_speaker_machine_identity_media_file "
    "ON speaker (media_file_id) "
    "WHERE identity_source IN ('voiceprint', 'voiceprint_link')"
)


def upgrade() -> None:
    # CONCURRENTLY cannot run in a transaction (PostgreSQL CREATE INDEX docs).
    with op.get_context().autocommit_block():
        op.execute(DROP_INVALID_SQL)
        op.execute(CREATE_SQL)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_speaker_machine_identity_media_file")
