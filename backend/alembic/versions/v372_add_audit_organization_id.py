"""Org attribution for audit events + creation-time tenant stamp on watch_source.

Issue #262 (a)/(c) — hardening the multi-tenant seams that shipped in PR #250.

**(a) Audit events.** Auth/admin audit events now carry a nullable
``organization_id``. The audit store is **OpenSearch** (``audit-logs-*``
indices, ``app/auth/audit.py``) — not a relational table — so the field lives
in the event schema/index mapping and needs NO DDL here. It is recorded in this
revision's docstring because this is where the schema-version history lives:
events written at or after v372 are org-stamped when the writing code has
tenant context; events written before (or without context) have no
``organization_id`` field, and the org-admin read attributes those *legacy*
events via the org's member user-ids instead (see ``query_audit_logs``).

**(c) watch_source.organization_id.** Background watch-source imports used to
*guess* the tenant at import time (owner's first active membership — see
``resolve_owner_org_id``). The org is now captured ONCE at source-creation time
from the creating request's context and stamped on every import. Existing rows
are backfilled with the same first-active-membership resolution the imports
were already receiving at runtime, so the backfill freezes (not changes) their
effective scope. Community edition: the membership table is empty, so the
column stays NULL (personal) everywhere.

All SQL is idempotent for safe re-run on partially-migrated databases; the
backfill only runs when the column is first created so a re-run never
re-guesses rows that were since edited.

Revision ID: v372_add_audit_organization_id
Revises: v371_repair_cloud_seams_columns
Create Date: 2026-07-03
"""

from alembic import op

revision = "v372_add_audit_organization_id"
down_revision = "v371_repair_cloud_seams_columns"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'watch_source' AND column_name = 'organization_id'
            ) THEN
                ALTER TABLE watch_source
                    ADD COLUMN organization_id INTEGER
                        REFERENCES organization(id) ON DELETE SET NULL;

                -- One-time backfill: freeze the first-active-membership guess the
                -- runtime resolver was already applying to these sources' imports.
                -- Runs ONLY when the column was just created (idempotent re-runs
                -- never overwrite later edits). No-op when memberships are empty
                -- (community edition) — rows stay NULL = personal scope.
                UPDATE watch_source ws
                SET organization_id = sub.org_id
                FROM (
                    SELECT DISTINCT ON (om.user_id)
                           om.user_id,
                           om.organization_id AS org_id
                    FROM organization_membership om
                    JOIN organization o
                      ON o.id = om.organization_id AND o.is_active
                    ORDER BY om.user_id, om.id ASC
                ) sub
                WHERE ws.user_id = sub.user_id;
            END IF;
        END $$;
    """)

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_watch_source_organization_id "
        "ON watch_source (organization_id) WHERE organization_id IS NOT NULL"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_watch_source_organization_id")
    op.execute("ALTER TABLE watch_source DROP COLUMN IF EXISTS organization_id")
