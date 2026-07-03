"""Repair the pre-release v367 schema shape + takedown prior-status column.

DBs that applied the ORIGINAL (unreleased) v367 — public master between
2026-06-05 and this release, and commercial deployments pinned to those
commits — carry vendor-named identity columns that the rewritten v367 no
longer creates and the ORM no longer maps. Because the v367 revision id was
reused, Alembic will not re-run it on those DBs, so this follow-up revision
performs an idempotent in-place repair:

  user.clerk_id             -> external_id      (unique partial index renamed)
  user.clerk_org_id         -> external_org_id
  organization.clerk_org_id -> external_org_id  (NOT NULL dropped, index renamed)

Legacy billing columns on organization (stripe_*, subscription_*, hours_*,
seats/billing anchors) are intentionally LEFT IN PLACE: the commercial layer
owns that data and relocates it to its own tables in its own alembic chain
before dropping. Community DBs have them at empty defaults — harmless dead
columns, never mapped by the ORM.

users_auth_type_check is normalized to the core auth types ONLY when no row
uses an external provider string; on commercial DBs the wider legacy
constraint is left for the commercial chain to manage (re-adding the narrow
constraint there would fail validation against existing rows).

Also adds media_file.pre_quarantine_status so a takedown release restores the
file's actual prior status instead of assuming COMPLETED.

Revision ID: v371_repair_cloud_seams_columns
Revises: v370_add_media_file_quarantine
"""

from alembic import op

revision = "v371_repair_cloud_seams_columns"
down_revision = "v370_add_media_file_quarantine"
branch_labels = None
depends_on = None

# (table, legacy column, target column) renames from the pre-release v367 shape.
_RENAMES = (
    ('"user"', "clerk_id", "external_id"),
    ('"user"', "clerk_org_id", "external_org_id"),
    ("organization", "clerk_org_id", "external_org_id"),
)


def _rename_sql(table: str, legacy: str, target: str) -> str:
    """Guarded rename: rename when only the legacy column exists; if both exist
    (half-repaired DB), backfill the target from the legacy column and drop it."""
    bare = table.strip('"')
    return f"""
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name = '{bare}' AND column_name = '{legacy}')
           AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name = '{bare}' AND column_name = '{target}') THEN
            ALTER TABLE {table} RENAME COLUMN {legacy} TO {target};
        ELSIF EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name = '{bare}' AND column_name = '{legacy}')
           AND EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name = '{bare}' AND column_name = '{target}') THEN
            -- PL/pgSQL parses statements lazily, so this branch only compiles
            -- (and the columns only need to exist) when it actually runs.
            UPDATE {table} SET {target} = {legacy}
            WHERE {target} IS NULL AND {legacy} IS NOT NULL;
            ALTER TABLE {table} DROP COLUMN {legacy};
        END IF;
    END $$;
    """


def upgrade():
    for table, legacy, target in _RENAMES:
        op.execute(_rename_sql(table, legacy, target))

    # Index renames: drop the legacy-named indexes, (re)create the new-shape
    # ones. IF NOT EXISTS makes both halves safe on fresh/new-shape DBs.
    op.execute("DROP INDEX IF EXISTS uq_user_clerk_id")
    op.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS uq_user_external_id ON "user" (external_id) '
        "WHERE external_id IS NOT NULL"
    )
    op.execute("DROP INDEX IF EXISTS uq_organization_clerk_org_id")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_organization_external_org_id "
        "ON organization (external_org_id)"
    )

    # The pre-release shape declared organization.clerk_org_id NOT NULL; the
    # generic column is nullable (an org row may precede its first external
    # mapping). DROP NOT NULL is a no-op when already nullable.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name = 'organization'
                         AND column_name = 'external_org_id'
                         AND is_nullable = 'NO') THEN
                ALTER TABLE organization ALTER COLUMN external_org_id DROP NOT NULL;
            END IF;
        END $$;
        """
    )

    # Normalize the auth_type CHECK to the core set only when it would validate
    # against existing rows; commercial DBs keep their wider constraint.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM "user"
                           WHERE auth_type NOT IN ('local', 'ldap', 'keycloak', 'pki')) THEN
                ALTER TABLE "user" DROP CONSTRAINT IF EXISTS users_auth_type_check;
                ALTER TABLE "user" ADD CONSTRAINT users_auth_type_check
                    CHECK (auth_type IN ('local', 'ldap', 'keycloak', 'pki'));
            END IF;
        END $$;
        """
    )

    # Takedown release restores the file's actual prior status (v370 follow-up).
    op.execute("ALTER TABLE media_file ADD COLUMN IF NOT EXISTS pre_quarantine_status VARCHAR(50)")


def downgrade():
    # The renames are a repair toward the canonical v367 shape — reversing them
    # would recreate the broken pre-release shape, so downgrade only removes the
    # additive column.
    op.execute("ALTER TABLE media_file DROP COLUMN IF EXISTS pre_quarantine_status")
