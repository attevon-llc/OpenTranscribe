"""At most one active task row per (file, step), and resolve the duplicates that exist (issue #1185).

Why
---
A late write from a superseded run could leave a ``task`` row ``in_progress`` forever after a
newer run of the same step had completed for the same file. Monitoring that counts long-running
rows then alerted indefinitely, and automatic recovery did nothing because the file was
completed and nothing was running. The race itself was closed by #1178; this revision is the
database half of the self-healing that covers any row that still slips through.

What
----
1. Resolve the rows that would violate the guard, with the same rule the recovery sweep applies
   (``app/services/task_row_supersede.py``):
   a. an active (``pending`` / ``in_progress``) row is marked ``failed`` with the reason
      ``superseded by <task id>`` when a NEWER row of the same ``task_type`` for the same
      ``media_file_id`` is ``completed``;
   b. where several active rows of one (file, step) remain and none of them has been
      superseded by a completed one, the newest is kept and the older ones are failed with
      ``superseded by <the kept id>``. Newest is by ``created_at``, then by ``id``.
   Rows are never deleted. Rows without a ``media_file_id`` (corpus-wide jobs) are untouched.
2. ``uq_task_active_per_file_type``: a partial UNIQUE index on ``(media_file_id, task_type)``
   for active rows. A second active row for one file and step is refused by the database.

The table is locked ``SHARE ROW EXCLUSIVE`` first (readers continue, writers wait) so no worker
can insert a violating row between the cleanup and the index build, which would fail the build
and abort startup. The lock is held for the two UPDATEs and a NON-concurrent index build, so
the time writers wait grows with the size of ``task``; nothing here measures that size, so it is
not assumed to be small. Two things bound the cost:

* ``lock_timeout`` (``LOCK_TIMEOUT``) applies to the ``LOCK TABLE`` only. If a long transaction
  already holds a conflicting lock, the statement would otherwise queue and every Celery write
  to ``task`` would queue behind IT; instead the upgrade fails after the timeout with a message
  naming the cause, and the next start retries. The caller's own ``lock_timeout`` is restored
  as soon as the lock is held, so the rest of the migration chain is unaffected.
* Operators of a large ``task`` table should run the upgrade in a quiet period (the
  upgrade guide says so).

Idempotent: both UPDATEs match nothing on a second run and the index is ``IF NOT EXISTS``.

Downgrade drops the index only. The rows the upgrade marked ``failed`` stay ``failed``: which
rows were active before is not recorded, and re-opening a row that was resolved as superseded
would recreate the exact stuck state this revision removes.

Revision ID: v448_task_one_active_row_per_file_step
Revises: v447_add_content_hold
Create Date: 2026-10-27
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v448_task_one_active_row_per_file_step"
down_revision = "v447_add_content_hold"
branch_labels = None
depends_on = None

INDEX_NAME = "uq_task_active_per_file_type"

#: How long the upgrade waits for the table lock before giving up with a clear message.
LOCK_TIMEOUT = "30s"


def build_upgrade_sql(lock_timeout: str = LOCK_TIMEOUT) -> str:
    """The upgrade statements; ``lock_timeout`` is a parameter so a test can use a short one."""
    return f"""
    DO $lock$
    DECLARE
        previous text := current_setting('lock_timeout');
    BEGIN
        PERFORM set_config('lock_timeout', '{lock_timeout}', true);
        BEGIN
            LOCK TABLE task IN SHARE ROW EXCLUSIVE MODE;
        EXCEPTION WHEN lock_not_available THEN
            RAISE EXCEPTION
                'v448 could not lock the task table within {lock_timeout} because another '
                'transaction holds a conflicting lock on it'
                USING HINT = 'Stop the Celery workers or end the long-running transaction, '
                             'then start the backend again; the upgrade is safe to retry.';
        END;
        PERFORM set_config('lock_timeout', previous, true);
    END
    $lock$;

    -- (a) active rows with a newer completed row of the same step for the same file
    WITH superseded AS (
        SELECT old.id AS old_id,
               (
                   SELECT n.id FROM task n
                   WHERE n.media_file_id = old.media_file_id
                     AND n.task_type = old.task_type
                     AND n.status = 'completed'
                     AND n.created_at > old.created_at
                   ORDER BY n.created_at DESC, n.id DESC
                   LIMIT 1
               ) AS by_id
        FROM task old
        WHERE old.status IN ('pending', 'in_progress')
          AND old.media_file_id IS NOT NULL
    )
    UPDATE task
    SET status = 'failed',
        error_message = 'superseded by ' || s.by_id,
        completed_at = COALESCE(task.completed_at, now()),
        updated_at = now()
    FROM superseded s
    WHERE task.id = s.old_id
      AND s.by_id IS NOT NULL;

    -- (b) several active rows left for one (file, step): keep the newest
    WITH ranked AS (
        SELECT id,
               first_value(id) OVER w AS keep_id,
               row_number() OVER w AS rn
        FROM task
        WHERE status IN ('pending', 'in_progress')
          AND media_file_id IS NOT NULL
        WINDOW w AS (
            PARTITION BY media_file_id, task_type
            ORDER BY created_at DESC NULLS LAST, id DESC
        )
    )
    UPDATE task
    SET status = 'failed',
        error_message = 'superseded by ' || r.keep_id,
        completed_at = COALESCE(task.completed_at, now()),
        updated_at = now()
    FROM ranked r
    WHERE task.id = r.id
      AND r.rn > 1;

    -- Created last: the detection arm keys on it.
    CREATE UNIQUE INDEX IF NOT EXISTS {INDEX_NAME}
        ON task (media_file_id, task_type)
        WHERE status IN ('pending', 'in_progress');
"""


#: Module-level so the consistency test replays the real statements.
UPGRADE_SQL = build_upgrade_sql()

DOWNGRADE_SQL = f"""
    DROP INDEX IF EXISTS {INDEX_NAME};
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
