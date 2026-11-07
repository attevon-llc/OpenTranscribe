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

THIS REVISION BUILDS NOTHING ITSELF. ``CREATE INDEX CONCURRENTLY`` waits for every transaction
that can still see ``speaker`` (PostgreSQL docs), so inside an upgrade a single idle-in-transaction
session or a long celery transaction would hang startup, and a performance index must never do
that. The runner builds it after the upgrade through ``app.db.concurrent_indexes`` (AUTOCOMMIT,
``SET lock_timeout``, INVALID leftover dropped first, never raises) and retries on every start
until it exists; ``app.db.speaker_indexes.SPEAKER_INDEXES`` holds the definition. A plain
``alembic upgrade head`` therefore leaves the index to the next application start.

The detection arm keys on the index. If it is still pending, an untracked database is stamped
v457 and this revision re-runs, which is a no-op: stamping low is harmless here because the
revision has no DDL to skip. The downgrade drops the index and destroys no data.

Revision ID: v458_speaker_machine_identity_index
Revises: v457_audit_guard_followups
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v458_speaker_machine_identity_index"
down_revision = "v457_audit_guard_followups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """No DDL: see the module docstring."""


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_speaker_machine_identity_media_file")
