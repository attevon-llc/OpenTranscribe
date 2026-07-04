"""Tenant scope for speaker clusters (issue #262, cluster plane).

PR #250 and the v372-era follow-ups org-scoped the per-file speaker docs,
speaker profiles, and voiceprint kNN queries, but ``speaker_cluster`` rows —
and their centroid docs in OpenSearch — stayed tenant-blind. The cluster kNN
gate (``find_matching_clusters``) already filters by org, so org-file speakers
could never JOIN a cluster (centroid docs carried no org field and the ``term``
filter matched nothing): isolation-safe but degraded to per-speaker singleton
clusters inside orgs.

This revision adds a nullable ``organization_id`` to ``speaker_cluster``
(NULL = personal scope, identical to every other tenant-stamped table). The
write path stamps it from the member speakers' FILE org at creation time; the
centroid doc in OpenSearch mirrors it (``store_cluster_embedding``). Existing
rows/docs are backfilled by ``app.tasks.tenant_backfill_task`` (all members'
files in one org -> stamp that org; mixed-org legacy clusters stay NULL and
are only counted — splitting them is deliberately out of scope).

COMMUNITY EDITION: every ``media_file.organization_id`` is NULL, so the column
stays NULL on every row and behavior is byte-identical (the personal
``must_not exists`` gate is a no-op on org-less docs).

All SQL is idempotent (``IF NOT EXISTS``) for safe re-run on
partially-migrated databases.

Revision ID: v373_add_cluster_organization_id
Revises: v372_add_audit_organization_id
Create Date: 2026-07-04
"""

from alembic import op

revision = "v373_add_cluster_organization_id"
down_revision = "v372_add_audit_organization_id"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'speaker_cluster' AND column_name = 'organization_id'
            ) THEN
                ALTER TABLE speaker_cluster
                    ADD COLUMN organization_id INTEGER REFERENCES organization(id);
            END IF;
        END $$;
    """)

    # Partial index: community rows are all NULL, so only org rows are indexed.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_speaker_cluster_organization_id "
        "ON speaker_cluster (organization_id) WHERE organization_id IS NOT NULL"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_speaker_cluster_organization_id")
    op.execute("ALTER TABLE speaker_cluster DROP COLUMN IF EXISTS organization_id")
