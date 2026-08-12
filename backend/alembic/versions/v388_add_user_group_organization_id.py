"""Add ``user_group.organization_id`` — the group plane had no tenant boundary at all.

Every other user-owned table carries an ``organization_id`` stamp (``media_file``,
``collection``, ``speaker``, ``chat_conversation``, ``summary_prompt``, …) and is filtered
through ``api/deps_context.scope_to_context``. ``user_group`` did not have the column, so
there was nothing for a tenant filter to read and ``endpoints/groups.py`` never had one:
``add_member`` resolved its target purely by UUID, which let a group admin in tenant A add
a member of tenant B and — because collections are shared with *groups* — hand them a
sharing surface that reaches across the tenant boundary.

Design notes worth keeping with the DDL:
  - **Nullable, NULL = personal scope.** Identical to every other org stamp, and the reason
    the community edition is behaviour-identical: ``organization`` and
    ``organization_membership`` are empty there, so every row stays NULL and
    ``scope_to_context``'s personal branch is the only one ever taken.
  - **Plain ``REFERENCES organization(id)``, no ``ON DELETE``.** The house rule for this
    stamp: 11 of the 12 FKs into ``organization`` are ``NO ACTION`` on purpose, so deleting
    a tenant cannot silently strip rows of their tenancy and re-expose them as personal
    data. Whole-tenant erasure is an explicit operation
    (``POST /org-admin/gdpr/erase-organization``), not a cascade.
  - **Partial index**, matching ``ix_chat_project_organization_id``: community rows are all
    NULL, so only org rows are indexed.
  - **Backfill from the owner's membership, when it is unambiguous.** Leaving existing rows
    NULL would be the simpler migration but not the safe one: in an org deployment every
    pre-existing group would become personal-scope, and would therefore vanish from its
    own members' listings the moment they work in org context. So a group whose owner
    belongs to exactly ONE organization is stamped with it. An owner in zero orgs (every
    community account) or in two or more is left NULL, because there is no answer the
    migration can derive rather than guess — a multi-org owner's group has to be re-scoped
    by a human who knows which tenant it was for.

All SQL is idempotent so it is safe to re-run against a partially-migrated database (the
startup runner stamps untracked databases by schema fingerprint, so a revision routinely
re-runs over its own partial output).
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v388_add_user_group_organization_id"
down_revision = "v387_actor_fks_and_tag_share_check"
branch_labels = None
depends_on = None

#: Module-level so ``tests/unit/test_v388_migration_consistency.py`` replays the real
#: statements instead of asserting on this file's source text — the shape ``v386``'s missing
#: test let through and ``v387`` established.
ADD_COLUMN_SQL = """
    ALTER TABLE user_group ADD COLUMN IF NOT EXISTS organization_id INTEGER;

    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'user_group_organization_id_fkey'
        ) THEN
            ALTER TABLE user_group
                ADD CONSTRAINT user_group_organization_id_fkey
                FOREIGN KEY (organization_id) REFERENCES organization(id);
        END IF;
    END $$;

    CREATE INDEX IF NOT EXISTS ix_user_group_organization_id
        ON user_group (organization_id) WHERE organization_id IS NOT NULL;
"""

#: Only rows that are still unstamped, and only owners with exactly one membership. Written
#: as a correlated subquery with a ``HAVING count(*) = 1`` guard so re-running it is a no-op
#: rather than a second, different answer.
BACKFILL_SQL = """
    UPDATE user_group g
       SET organization_id = (
               SELECT m.organization_id
                 FROM organization_membership m
                WHERE m.user_id = g.owner_id
             GROUP BY m.organization_id
               HAVING count(*) = 1
           )
     WHERE g.organization_id IS NULL
       AND (SELECT count(DISTINCT m.organization_id)
              FROM organization_membership m
             WHERE m.user_id = g.owner_id) = 1;
"""

UPGRADE_SQL = ADD_COLUMN_SQL + BACKFILL_SQL

#: The column goes, so the backfill has nothing to mirror.
DOWNGRADE_SQL = """
    DROP INDEX IF EXISTS ix_user_group_organization_id;
    ALTER TABLE user_group DROP CONSTRAINT IF EXISTS user_group_organization_id_fkey;
    ALTER TABLE user_group DROP COLUMN IF EXISTS organization_id;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
