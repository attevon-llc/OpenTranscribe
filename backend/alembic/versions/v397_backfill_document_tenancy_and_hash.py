"""Backfill ``document.organization_id`` from watch-source imports; mark the
document-tenancy fix complete (#362 follow-up, lane C0).

``api/endpoints/documents.py`` depended on ``get_current_active_user`` rather than
``get_current_context`` — every document ever created through the manual-upload
endpoint carries ``organization_id = NULL`` regardless of the uploader's active org,
which is BOTH a leak (a personal-scope query can see it once org gating is
enforced elsewhere) and an over-restriction (an org's own document becomes
unreachable from org scope). That endpoint bug is fixed in code by this same change
(threading ``RequestContext`` through and stamping ``organization_id`` at creation);
this revision is the data half.

Design notes:
  - **Only watch-sourced documents are backfilled, and that is deliberate, not a
    shortcut.** ``services/watch_sources/document_ingest.py::finalize_document_ingest``
    has ALWAYS stamped ``organization_id`` from ``WatchSource.organization_id`` at
    creation time — the tenant scope is captured on the source when the operator
    configures it, never inferred later (the same rule
    ``watch_sources/CLAUDE.md`` states for media: issue #262c). So this UPDATE is a
    defensive repair for any row that predates that code path or reached this table
    through a bug, not a routine expected to touch many rows.
  - **Manually-uploaded documents are NOT backfilled from the uploader's current org
    membership**, on purpose. The manual-upload endpoint had no org concept at all
    before this change, so there is no recorded signal for what scope the upload was
    "supposed" to be in — inferring one from the user's org membership TODAY would
    violate the same house rule the watch-source path already follows (tenant scope
    is captured at creation, never inferred from current state) and risks moving a
    document a user still expects to find in their personal library into an org's
    shared space. Leaving ``organization_id`` NULL keeps it in personal scope, which
    is the safe direction: visible only to its owner, never to a wider org audience
    it was never scoped to.
  - **``document.file_hash`` is NOT computed here.** Populating it needs a real read
    of each document's stored bytes (imohash's sampled windows via MinIO ranged
    reads), and this codebase's own precedent for that class of backfill —
    ``app/tasks/imohash_recompute.py``, written for the identical column on
    ``media_file`` — is a self-contained Celery task, never inline migration SQL:
    a schema migration that reaches out to object storage risks failing (or hanging)
    the startup migration runner on a MinIO outage, which `SystemExit(1)`-aborts the
    whole backend rather than serving a half-migrated schema
    (``app/db/CLAUDE.md``). The document-plane equivalent is
    ``app.tasks.document_tasks.backfill_document_file_hashes`` — run once, the same
    way ``tenant_backfill_task.py`` documents its own one-off invocation.
  - **The completion marker is a ``system_settings`` row, not a schema object.**
    Nothing about this revision's work (a conditional ``UPDATE`` against existing
    rows) leaves a distinguishing column or constraint behind the way a DDL
    revision would, so — mirroring ``v379_rename_keycloak_config_to_oidc``'s
    "pure data migration, no DDL" shape — the detection arm needs an explicit
    fingerprint rather than an implicit one. A settings row is cheaper and more
    direct here than v379's "probe the absence of retired data" trick: it is a
    normal, idempotent, ``ON CONFLICT DO NOTHING`` insert, exactly like the
    seed rows several early revisions already write.

Revision ID: v397_backfill_document_tenancy_and_hash
Revises: v396_add_document_chunk_redaction_cache
Create Date: 2026-08-19
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v397_backfill_document_tenancy_and_hash"
down_revision = "v396_add_document_chunk_redaction_cache"
branch_labels = None
depends_on = None

#: Module-level so the consistency test can replay it against seeded rows instead of
#: asserting on this file's source text (the v387/v388/v390/v394 convention).
BACKFILL_ORGANIZATION_ID_SQL = """
    UPDATE document AS d
    SET organization_id = ws.organization_id
    FROM watch_source_file AS wsf
    JOIN watch_source AS ws ON ws.id = wsf.watch_source_id
    WHERE wsf.document_id = d.id
      AND d.organization_id IS NULL
      AND ws.organization_id IS NOT NULL;
"""

MARK_COMPLETE_SQL = """
    INSERT INTO system_settings (key, value, description) VALUES
        ('documents.tenancy_backfill_v397', 'true',
         'Marker: v397 has backfilled document.organization_id from watch-source '
         'imports. Read only by the migration detection ladder; not a runtime '
         'toggle.')
    ON CONFLICT (key) DO NOTHING;
"""

UPGRADE_SQL = BACKFILL_ORGANIZATION_ID_SQL + MARK_COMPLETE_SQL

#: Additive-only downgrade, matching v371's documented precedent: the marker is
#: removed so a downgrade-then-upgrade cycle re-runs the backfill (harmless — it is
#: idempotent, `d.organization_id IS NULL` is the only rows it ever touches), but the
#: organization_id values themselves are NOT reset to NULL. Undoing a tenancy
#: correction is not a schema downgrade's job, and the rows it touched already held
#: NULL before this revision existed, so there is nothing destructive being
#: preserved by leaving them alone.
DOWNGRADE_SQL = """
    DELETE FROM system_settings WHERE key = 'documents.tenancy_backfill_v397';
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
