"""Tenant-owned collections (issue #1051).

``collection.organization_id`` has existed for a while and every read path gates
on it, but nothing ever **wrote** it:

* ``POST /api/collections`` created every collection as personal, so a
  collection made while working in an organization vanished from that org's
  list and showed up in the creator's personal workspace instead.
* The AI auto-labeler matched suggestions against *all* of a file owner's
  collections, so one collection ended up holding recordings from several
  tenants, and ``UNIQUE (user_id, name)`` meant a user could not have "Board" in
  two organizations.

The owner decision is that **inside an organization a collection is shared by
the organization** (the same rule as tags, ``v420``); a personal-workspace
collection stays its owner's. After this revision a collection belongs to
exactly one tenant:

=============  ===================  ======================  =========================
kind           ``organization_id``  ``user_id``             visible to
=============  ===================  ======================  =========================
personal       NULL                 the owner               the owner (plus shares)
organization   the org              creator (nullable)      every member, in that org
=============  ===================  ======================  =========================

``user_id`` on an organization collection is attribution: the creator (with an
org admin) may delete it and manage its explicit shares, every other member is an
editor. It becomes NULL when the creator's account is deleted or erased — the
collection is the tenant's and stays with the tenant.

Schema
------
* ``collection.user_id`` becomes NULLABLE, guarded by
  ``ck_collection_owner_or_org`` (``user_id IS NOT NULL OR organization_id IS NOT
  NULL``) — a personal collection always has an owner.
* ``UNIQUE (user_id, name)`` is replaced by one partial unique index per kind:
  ``uq_collection_user_name`` ``(user_id, name) WHERE organization_id IS NULL``
  and ``uq_collection_org_name`` ``(organization_id, name) WHERE organization_id
  IS NOT NULL``. The old constraint is found by its columns, not by name: live
  databases carry ``collection_user_id_name_key`` while the ORM declared
  ``_user_collection_uc``.
* ``collection.organization_id`` and its FK (no ``ON DELETE``, the house rule for
  org stamps) already exist and are unchanged.

Backfill (``BACKFILL_SQL``; re-runnable, each phase converges)
--------------------------------------------------------------
A collection's tenants are the ``organization_id`` values of its member files
(NULL = a personal file). The rule assigns a collection to the tenant of its
members where that is unambiguous and splits it where it is not:

1. **Move.** A personal collection whose members are *all* organization files
   moves into the lowest-id organization among them (normally the only one). Its
   id, shares and default prompt are unchanged.
2. **Split.** Every remaining (collection, member-file tenant) mismatch gets the
   collection's row in that tenant — an existing collection of the same name there
   if there is one (which is what folds two members' same-named collections
   together), otherwise a copy carrying the name, description, visibility, source,
   default prompt and original creator — and that tenant's ``collection_member``
   rows move to it. A file already in the target keeps its row and the duplicate
   is dropped. Explicit shares are **not** copied: an organization's members reach
   its collections by membership, and a share on the original could name someone
   outside that organization.
3. **Merge.** An organization left holding two collections of one name (two
   members' collections moved in by phase 1) collapses onto the lowest id: members
   and shares move (duplicates dropped), an empty description/default prompt is
   filled from the merged row, and the merged row is deleted.

A collection with no members stays personal: it carries no evidence of the tenant
it was made in, and guessing an organization would publish a private name to every
member of it. The same holds for a collection whose members are all personal.

Merging is on the exact stored ``name`` (the unit the unique index enforces).

COMMUNITY EDITION: ``organization`` is empty and every file is personal, so no
phase fires; the only changes are the nullable column, the check constraint and
the unique-index shape (same rule for personal rows as before).

Downgrade
---------
Drops the check constraint and the two partial indexes and restores
``UNIQUE (user_id, name)`` and ``NOT NULL`` — each only when the data still
satisfies it (an unattributed org collection or one name held in two tenants
cannot be represented in the old shape; they are left alone rather than deleted or
re-merged across tenants, the same partial-downgrade stance as ``v374``/``v420``).

Revision ID: v422_add_collection_tenancy
Revises: v421_add_media_playback_path
Create Date: 2026-09-28
"""

from alembic import op

revision = "v422_add_collection_tenancy"
down_revision = "v421_add_media_playback_path"
branch_labels = None
depends_on = None

#: Module-level so ``tests/unit/test_v422_migration_consistency.py`` replays the
#: real statements rather than asserting on source text.
#:
#: The old per-user unique would reject phase 2's copies (one user, one name, two
#: tenants), so it is dropped before the backfill; the replacements are created
#: after it.
PREPARE_SQL = """
    ALTER TABLE collection ALTER COLUMN user_id DROP NOT NULL;

    DO $$
    DECLARE
        con RECORD;
    BEGIN
        FOR con IN
            SELECT c.conname
              FROM pg_constraint c
             WHERE c.conrelid = 'collection'::regclass
               AND c.contype = 'u'
               AND (SELECT array_agg(a.attname::text ORDER BY a.attname)
                      FROM unnest(c.conkey) AS k(attnum)
                      JOIN pg_attribute a
                        ON a.attrelid = c.conrelid AND a.attnum = k.attnum)
                   = ARRAY['name', 'user_id']
        LOOP
            EXECUTE format('ALTER TABLE collection DROP CONSTRAINT %I', con.conname);
        END LOOP;
    END $$;
"""

BACKFILL_SQL = """
    DO $$
    DECLARE
        rec RECORD;
        target_id INTEGER;
    BEGIN
        -- Phase 1: move a personal collection whose members are all organization
        -- files into the lowest-id of those organizations. Keeps the row id.
        UPDATE collection c
           SET organization_id = usage.min_org
          FROM (
                SELECT cm.collection_id,
                       MIN(mf.organization_id) AS min_org,
                       BOOL_OR(mf.organization_id IS NULL) AS has_personal
                  FROM collection_member cm
                  JOIN media_file mf ON mf.id = cm.media_file_id
                 GROUP BY cm.collection_id
               ) AS usage
         WHERE c.id = usage.collection_id
           AND c.organization_id IS NULL
           AND NOT usage.has_personal
           AND usage.min_org IS NOT NULL;

        -- Phase 2: split every remaining (collection, member tenant) mismatch. An
        -- organization target is ANY collection of that name there, so members'
        -- same-named collections converge; a personal target belongs to the
        -- collection's creator (or, for an unattributed org collection, the file's
        -- owner).
        FOR rec IN
            SELECT DISTINCT
                   c.id AS source_id,
                   c.user_id AS coll_user_id,
                   c.name,
                   mf.organization_id AS file_org,
                   CASE WHEN mf.organization_id IS NULL
                        THEN COALESCE(c.user_id, mf.user_id)
                        ELSE c.user_id
                   END AS owner_id
              FROM collection_member cm
              JOIN media_file mf ON mf.id = cm.media_file_id
              JOIN collection c ON c.id = cm.collection_id
             WHERE c.organization_id IS DISTINCT FROM mf.organization_id
             ORDER BY source_id, file_org NULLS FIRST
        LOOP
            IF rec.file_org IS NOT NULL THEN
                SELECT id INTO target_id
                  FROM collection
                 WHERE organization_id = rec.file_org AND name = rec.name
                 ORDER BY id
                 LIMIT 1;
            ELSE
                SELECT id INTO target_id
                  FROM collection
                 WHERE organization_id IS NULL
                   AND user_id = rec.owner_id
                   AND name = rec.name
                 ORDER BY id
                 LIMIT 1;
            END IF;

            IF target_id IS NULL THEN
                INSERT INTO collection (uuid, name, description, user_id, organization_id,
                                        is_public, default_summary_prompt_id, source)
                SELECT gen_random_uuid(), src.name, src.description, rec.owner_id,
                       rec.file_org, src.is_public, src.default_summary_prompt_id, src.source
                  FROM collection src
                 WHERE src.id = rec.source_id
                RETURNING id INTO target_id;
            END IF;

            -- A file already in the target keeps that membership. An unattributed
            -- org collection's personal files are split per file owner, so both
            -- statements are narrowed to this owner's files in that case.
            DELETE FROM collection_member cm
             USING media_file mf
             WHERE cm.media_file_id = mf.id
               AND cm.collection_id = rec.source_id
               AND mf.organization_id IS NOT DISTINCT FROM rec.file_org
               AND (rec.file_org IS NOT NULL OR rec.coll_user_id IS NOT NULL
                    OR mf.user_id = rec.owner_id)
               AND EXISTS (SELECT 1 FROM collection_member other
                            WHERE other.media_file_id = cm.media_file_id
                              AND other.collection_id = target_id);

            UPDATE collection_member cm
               SET collection_id = target_id
              FROM media_file mf
             WHERE cm.media_file_id = mf.id
               AND cm.collection_id = rec.source_id
               AND mf.organization_id IS NOT DISTINCT FROM rec.file_org
               AND (rec.file_org IS NOT NULL OR rec.coll_user_id IS NOT NULL
                    OR mf.user_id = rec.owner_id);
        END LOOP;

        -- Phase 3: collapse same-named collections inside one organization onto
        -- the lowest id (members' collections that phase 1 moved into one org).
        FOR rec IN
            SELECT c.id AS doomed_id, keep.keep_id
              FROM collection c
              JOIN (
                    SELECT organization_id, name, MIN(id) AS keep_id
                      FROM collection
                     WHERE organization_id IS NOT NULL
                     GROUP BY organization_id, name
                    HAVING COUNT(*) > 1
                   ) AS keep
                ON keep.organization_id = c.organization_id
               AND keep.name = c.name
             WHERE c.id <> keep.keep_id
        LOOP
            DELETE FROM collection_member cm
             WHERE cm.collection_id = rec.doomed_id
               AND EXISTS (SELECT 1 FROM collection_member other
                            WHERE other.media_file_id = cm.media_file_id
                              AND other.collection_id = rec.keep_id);
            UPDATE collection_member SET collection_id = rec.keep_id
             WHERE collection_id = rec.doomed_id;

            DELETE FROM collection_share s
             WHERE s.collection_id = rec.doomed_id
               AND EXISTS (SELECT 1 FROM collection_share k
                            WHERE k.collection_id = rec.keep_id
                              AND (k.target_user_id = s.target_user_id
                                   OR k.target_group_id = s.target_group_id));
            UPDATE collection_share SET collection_id = rec.keep_id
             WHERE collection_id = rec.doomed_id;

            UPDATE collection keep
               SET description = COALESCE(keep.description, doomed.description),
                   default_summary_prompt_id = COALESCE(keep.default_summary_prompt_id,
                                                        doomed.default_summary_prompt_id)
              FROM collection doomed
             WHERE keep.id = rec.keep_id AND doomed.id = rec.doomed_id;

            DELETE FROM collection WHERE id = rec.doomed_id;
        END LOOP;
    END $$;
"""

#: Created after the backfill so a half-migrated database cannot trip the new
#: per-tenant uniqueness mid-split. ``uq_collection_org_name`` is last: the
#: detection arm keys on it.
FINALIZE_SQL = """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'ck_collection_owner_or_org'
        ) THEN
            ALTER TABLE collection
                ADD CONSTRAINT ck_collection_owner_or_org
                CHECK (user_id IS NOT NULL OR organization_id IS NOT NULL);
        END IF;
    END $$;

    CREATE UNIQUE INDEX IF NOT EXISTS uq_collection_user_name
        ON collection (user_id, name) WHERE organization_id IS NULL;
    CREATE UNIQUE INDEX IF NOT EXISTS uq_collection_org_name
        ON collection (organization_id, name) WHERE organization_id IS NOT NULL;
"""

UPGRADE_SQL = PREPARE_SQL + BACKFILL_SQL + FINALIZE_SQL

DOWNGRADE_SQL = """
    DROP INDEX IF EXISTS uq_collection_org_name;
    DROP INDEX IF EXISTS uq_collection_user_name;
    ALTER TABLE collection DROP CONSTRAINT IF EXISTS ck_collection_owner_or_org;

    -- The old shape, restored only where the data still satisfies it: an
    -- unattributed org collection cannot be NOT NULL, and a user may now hold one
    -- name in two tenants. Neither is deleted or re-merged across tenants.
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM collection WHERE user_id IS NULL) THEN
            ALTER TABLE collection ALTER COLUMN user_id SET NOT NULL;
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM collection GROUP BY user_id, name HAVING COUNT(*) > 1
        ) AND NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'collection_user_id_name_key'
        ) THEN
            ALTER TABLE collection
                ADD CONSTRAINT collection_user_id_name_key UNIQUE (user_id, name);
        END IF;
    END $$;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
