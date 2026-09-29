"""Tenant-owned tags — ``tag.organization_id`` (issue #1050).

``v374_add_tag_user_id`` gave every tag an owner but no tenant. In a deployment
with organizations that had two consequences:

* **A user's tags followed them across tenants.** Resolution and listing were
  keyed on ``user_id`` alone, so a tag coined in organization A — including one
  the auto-labeler derived from A's transcripts — appeared in the same user's
  picker in organization B and in their personal workspace.
* **Members of one organization duplicated each other's vocabulary.** Alice's
  "Q3 Board" and Bob's "Q3 Board" were two rows neither could see or reuse.

The owner decision is that **tags are shared within an organization**. After
this revision a tag belongs to exactly one tenant:

=============  ===================  ====================  =========================
kind           ``organization_id``  ``user_id``           visible to
=============  ===================  ====================  =========================
system         NULL                 NULL                  every tenant
personal       NULL                 the owner             the owner's personal space
organization   the org              creator (nullable)    every member, in that org
=============  ===================  ====================  =========================

``user_id`` on an organization tag is attribution only (who coined it — the
creator may rename it, as may an org admin); it does not scope visibility, and
it may be NULL once the creator's account is gone, so "system" is now
``user_id IS NULL AND organization_id IS NULL`` — never ``user_id IS NULL``
alone.

Schema
------
* ``tag.organization_id`` NULLABLE FK to ``organization(id)``, plain
  ``REFERENCES`` with no ``ON DELETE`` (the house rule every org stamp follows:
  deleting a tenant must not silently re-expose its rows as personal data;
  whole-tenant erasure deletes the org's tags explicitly).
* Uniqueness is per tenant: ``uq_tag_user_name`` is narrowed to personal rows
  (``organization_id IS NULL``), ``uq_tag_system_name`` to true system rows, and
  ``uq_tag_org_name`` = ``UNIQUE (organization_id, name) WHERE organization_id
  IS NOT NULL`` is added. The two existing index names are kept on purpose —
  every reader that names them keeps working; only their predicates move.

Backfill (``BACKFILL_SQL``; re-runnable, each phase converges)
--------------------------------------------------------------
Only non-system tags are touched. A tag's tenants are the ``organization_id``
values of the files it is attached to (NULL = a personal file).

1. **Move.** A personal tag attached *only* to organization files moves into the
   lowest-id organization among them. Its id is unchanged, so external
   references stay valid.
2. **Split.** Every remaining (tag, file-tenant) mismatch gets the tag's row in
   that tenant — an existing row with the same name if there is one (which is
   what folds two members' duplicates together), otherwise a copy carrying the
   same name/source/normalization and the original creator — and that tenant's
   ``file_tag`` rows are repointed at it. A file that already carries the
   target keeps its row and the duplicate association is dropped.
3. **Merge.** Any tenant left holding two rows of one name (two members' tags
   moved into the same org by phase 1) is collapsed onto its lowest-id row, with
   the same association handling. The merged-away rows are deleted;
   ``tag_share`` rows on them go with them (``ON DELETE CASCADE``).

Tags attached to nothing stay personal. An unattached tag carries no evidence
of the tenant it was made in, and guessing an organization would publish a
private word to every member of it.

Merging is on the exact stored ``name`` (the unit the unique index enforces).
Case/spelling variants inside one tenant are left for the existing collision
view (``GET /tags/collisions``) — a human decides those, as everywhere else.

COMMUNITY EDITION: ``organization`` is empty, every file is personal, so no
phase fires and the only change is the index predicates.

All SQL is idempotent so the startup runner can re-run it over its own partial
output.

Downgrade
---------
Drops the column and ``uq_tag_org_name`` and restores v374's predicates.
Deliberately **partial**: it does not re-merge the per-tenant copies phase 2
created (that would re-share one tenant's row with another), and if a user now
owns two same-named rows it leaves ``uq_tag_user_name`` off rather than fail,
exactly as ``v374``'s own downgrade does for ``UNIQUE (name)``.

Revision ID: v420_add_tag_organization_id
Revises: v397_add_platform_super_admin_link_authorized
Create Date: 2026-09-28
"""

from alembic import op

revision = "v420_add_tag_organization_id"
down_revision = "v397_add_platform_super_admin_link_authorized"
branch_labels = None
depends_on = None

#: Module-level so ``tests/unit/test_v420_migration_consistency.py`` replays the
#: real statements (the v387/v388 shape) rather than asserting on source text.
ADD_COLUMN_SQL = """
    ALTER TABLE tag ADD COLUMN IF NOT EXISTS organization_id INTEGER;

    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'tag_organization_id_fkey'
        ) THEN
            ALTER TABLE tag
                ADD CONSTRAINT tag_organization_id_fkey
                FOREIGN KEY (organization_id) REFERENCES organization(id);
        END IF;
    END $$;

    CREATE INDEX IF NOT EXISTS ix_tag_organization_id
        ON tag (organization_id) WHERE organization_id IS NOT NULL;
"""

#: The old per-user index would reject phase 2's copies (one user, one name, two
#: tenants), so it is dropped before the backfill and re-created after it.
DROP_OLD_UNIQUES_SQL = """
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM pg_indexes
             WHERE indexname = 'uq_tag_user_name'
               AND indexdef NOT LIKE '%organization_id IS NULL%'
        ) THEN
            DROP INDEX uq_tag_user_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_indexes
             WHERE indexname = 'uq_tag_system_name'
               AND indexdef NOT LIKE '%organization_id IS NULL%'
        ) THEN
            DROP INDEX uq_tag_system_name;
        END IF;
    END $$;
"""

BACKFILL_SQL = """
    DO $$
    DECLARE
        rec RECORD;
        target_id INTEGER;
    BEGIN
        -- Phase 1: move a personal tag used only on organization files into the
        -- lowest-id of those organizations. Keeps the row id.
        UPDATE tag t
           SET organization_id = usage.min_org
          FROM (
                SELECT ft.tag_id,
                       MIN(mf.organization_id) AS min_org,
                       BOOL_OR(mf.organization_id IS NULL) AS has_personal
                  FROM file_tag ft
                  JOIN media_file mf ON mf.id = ft.media_file_id
                 GROUP BY ft.tag_id
               ) AS usage
         WHERE t.id = usage.tag_id
           AND t.user_id IS NOT NULL
           AND t.organization_id IS NULL
           AND NOT usage.has_personal
           AND usage.min_org IS NOT NULL;

        -- Phase 2: split every remaining (tag, file tenant) mismatch. The target
        -- in an organization is ANY row of that name there, so two members'
        -- duplicates converge on one row; a personal target belongs to the tag's
        -- creator (or, for an unattributed org tag, the file's owner).
        FOR rec IN
            SELECT DISTINCT
                   t.id AS source_tag_id,
                   t.user_id AS tag_user_id,
                   t.name,
                   t.source,
                   t.normalized_name,
                   mf.organization_id AS file_org,
                   CASE WHEN mf.organization_id IS NULL
                        THEN COALESCE(t.user_id, mf.user_id)
                        ELSE t.user_id
                   END AS owner_id
              FROM file_tag ft
              JOIN media_file mf ON mf.id = ft.media_file_id
              JOIN tag t ON t.id = ft.tag_id
             WHERE NOT (t.user_id IS NULL AND t.organization_id IS NULL)
               AND t.organization_id IS DISTINCT FROM mf.organization_id
             ORDER BY source_tag_id, file_org NULLS FIRST
        LOOP
            IF rec.file_org IS NOT NULL THEN
                SELECT id INTO target_id
                  FROM tag
                 WHERE organization_id = rec.file_org AND name = rec.name
                 ORDER BY id
                 LIMIT 1;
            ELSE
                SELECT id INTO target_id
                  FROM tag
                 WHERE organization_id IS NULL
                   AND user_id = rec.owner_id
                   AND name = rec.name
                 ORDER BY id
                 LIMIT 1;
            END IF;

            IF target_id IS NULL THEN
                INSERT INTO tag (uuid, name, source, normalized_name, user_id, organization_id)
                VALUES (gen_random_uuid(), rec.name, rec.source, rec.normalized_name,
                        rec.owner_id, rec.file_org)
                RETURNING id INTO target_id;
            END IF;

            -- A file already carrying the target keeps that association. An
            -- unattributed org tag on personal files is split per file owner, so
            -- those two statements are narrowed to this owner's files.
            DELETE FROM file_tag ft
             USING media_file mf
             WHERE ft.media_file_id = mf.id
               AND ft.tag_id = rec.source_tag_id
               AND mf.organization_id IS NOT DISTINCT FROM rec.file_org
               AND (rec.file_org IS NOT NULL OR rec.tag_user_id IS NOT NULL
                    OR mf.user_id = rec.owner_id)
               AND EXISTS (SELECT 1 FROM file_tag other
                            WHERE other.media_file_id = ft.media_file_id
                              AND other.tag_id = target_id);

            UPDATE file_tag ft
               SET tag_id = target_id
              FROM media_file mf
             WHERE ft.media_file_id = mf.id
               AND ft.tag_id = rec.source_tag_id
               AND mf.organization_id IS NOT DISTINCT FROM rec.file_org
               AND (rec.file_org IS NOT NULL OR rec.tag_user_id IS NOT NULL
                    OR mf.user_id = rec.owner_id);
        END LOOP;

        -- Phase 3: collapse same-named rows inside one organization onto the
        -- lowest id (members' tags that phase 1 moved into the same org).
        FOR rec IN
            SELECT t.id AS doomed_id, keep.keep_id
              FROM tag t
              JOIN (
                    SELECT organization_id, name, MIN(id) AS keep_id
                      FROM tag
                     WHERE organization_id IS NOT NULL
                     GROUP BY organization_id, name
                    HAVING COUNT(*) > 1
                   ) AS keep
                ON keep.organization_id = t.organization_id
               AND keep.name = t.name
             WHERE t.id <> keep.keep_id
        LOOP
            DELETE FROM file_tag ft
             WHERE ft.tag_id = rec.doomed_id
               AND EXISTS (SELECT 1 FROM file_tag other
                            WHERE other.media_file_id = ft.media_file_id
                              AND other.tag_id = rec.keep_id);
            UPDATE file_tag SET tag_id = rec.keep_id WHERE tag_id = rec.doomed_id;
            DELETE FROM tag WHERE id = rec.doomed_id;
        END LOOP;
    END $$;
"""

#: Created after the backfill so a half-migrated database cannot trip the new
#: per-tenant uniqueness mid-split.
CREATE_UNIQUES_SQL = """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_tag_user_name
        ON tag (user_id, name) WHERE user_id IS NOT NULL AND organization_id IS NULL;
    CREATE UNIQUE INDEX IF NOT EXISTS uq_tag_system_name
        ON tag (name) WHERE user_id IS NULL AND organization_id IS NULL;
    CREATE UNIQUE INDEX IF NOT EXISTS uq_tag_org_name
        ON tag (organization_id, name) WHERE organization_id IS NOT NULL;
"""

UPGRADE_SQL = ADD_COLUMN_SQL + DROP_OLD_UNIQUES_SQL + BACKFILL_SQL + CREATE_UNIQUES_SQL

DOWNGRADE_SQL = """
    DROP INDEX IF EXISTS uq_tag_org_name;
    DROP INDEX IF EXISTS ix_tag_organization_id;
    DROP INDEX IF EXISTS uq_tag_user_name;
    DROP INDEX IF EXISTS uq_tag_system_name;
    ALTER TABLE tag DROP CONSTRAINT IF EXISTS tag_organization_id_fkey;
    ALTER TABLE tag DROP COLUMN IF EXISTS organization_id;

    -- v374's predicates, restored only where the data still satisfies them: an
    -- unattributed org tag reads back as a system row, and a user may now own a
    -- name in two tenants. Re-merging those would re-share one tenant's row with
    -- another, so the index is left off instead (v374's downgrade does the same).
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM tag WHERE user_id IS NOT NULL
             GROUP BY user_id, name HAVING COUNT(*) > 1
        ) THEN
            CREATE UNIQUE INDEX IF NOT EXISTS uq_tag_user_name
                ON tag (user_id, name) WHERE user_id IS NOT NULL;
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM tag WHERE user_id IS NULL GROUP BY name HAVING COUNT(*) > 1
        ) THEN
            CREATE UNIQUE INDEX IF NOT EXISTS uq_tag_system_name
                ON tag (name) WHERE user_id IS NULL;
        END IF;
    END $$;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
