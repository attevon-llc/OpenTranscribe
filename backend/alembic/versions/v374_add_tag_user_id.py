"""Per-user tag ownership — closes a cross-user tag-name disclosure.

SECURITY FIX. ``tag`` was a globally shared vocabulary *by schema*: ``name``
carried a global ``UNIQUE`` constraint and the table had no owner column, so
``_get_or_create_tag()`` reused any row by name and ``GET /api/tags/unused``
(``db.query(Tag).filter(~Tag.id.in_(used_tag_ids))``) returned **every**
unattached tag in the deployment to **any** authenticated user. Tag names are
user-authored free text ("Project Falcon Layoffs", a client's name, a case
number), so the listing leaked one tenant's vocabulary to every other account.
``GET /api/tags`` leaked the same set through its ``MediaFile.id IS NULL`` arm.

This revision gives ``tag`` an owner:

* ``tag.user_id`` NULLABLE FK to ``"user"(id)``.
  - NULL      = **system tag** — the seeded ``Important`` / ``Meeting`` /
                ``Interview`` / ``Personal`` vocabulary, visible to everyone.
  - NOT NULL  = that user's private tag.
* The global ``UNIQUE (name)`` is replaced by two partial unique indexes:
  ``uq_tag_user_name`` — ``UNIQUE (user_id, name) WHERE user_id IS NOT NULL``
  (two users may each own a "Meeting"), and ``uq_tag_system_name`` —
  ``UNIQUE (name) WHERE user_id IS NULL`` (the system vocabulary keeps the
  one-row-per-name guarantee the seeder relies on; a plain composite UNIQUE
  would not, because Postgres treats NULLs as distinct).

BACKFILL (two phases, both re-runnable):

1. Every tag attached to at least one file is claimed by the **lowest-numbered
   owning user** (``MIN(media_file.user_id)``) — deterministic, so a re-run or a
   replica converges on the same assignment.
2. MIXED-OWNERSHIP TAGS (the same tag row attached to files owned by two or more
   users, which the old global vocabulary made routine): every *other* owning
   user gets their **own copy** of the row — same name/source/normalized_name,
   fresh uuid — and only that user's ``file_tag`` rows are repointed at it. No
   file loses a tag and no user inherits another user's row. Phase 1's owner
   keeps the original ``tag.id``, so any external reference to it stays valid.

Tags attached to **no** file stay NULL, i.e. become system tags. That is what
keeps the seeded defaults in every user's picker after the split. A seeded
default that *was* in use (someone tagged a file "Meeting") is claimed by that
user like any other attached tag; ``_ensure_default_tags`` re-creates the
ownerless row on the next backend start, so the shared vocabulary is whole
again and the claiming user keeps their attachment. That is why the seeder's
lookup must carry ``user_id IS NULL`` (see ``app/initial_data.py``).

BREAKING for anyone reading ``tag`` directly: ``name`` is no longer globally
unique, so ``SELECT ... FROM tag WHERE name = ?`` can now return several rows.
Every in-repo read is scoped by owner or joined through ``file_tag``.

COMMUNITY EDITION: single-user deployments have exactly one owning user, so
phase 2 never fires and the only visible change is that attached tags gain that
user's id.

All SQL is idempotent (``IF NOT EXISTS`` / ``IF EXISTS`` / existence-guarded
``DO $$`` blocks) so it is safe to re-run against a partially-migrated database.

Revision ID: v374_add_tag_user_id
Revises: v373_add_cluster_organization_id
Create Date: 2026-08-05
"""

from alembic import op

revision = "v374_add_tag_user_id"
down_revision = "v373_add_cluster_organization_id"
branch_labels = None
depends_on = None

# Module-level so the consistency test can replay it against seeded
# mixed-ownership rows without duplicating the SQL.
BACKFILL_SQL = """
    DO $$
    DECLARE
        rec RECORD;
        copy_id INTEGER;
    BEGIN
        UPDATE tag t
           SET user_id = owners.owner_id
          FROM (
                SELECT ft.tag_id, MIN(mf.user_id) AS owner_id
                  FROM file_tag ft
                  JOIN media_file mf ON mf.id = ft.media_file_id
                 WHERE mf.user_id IS NOT NULL
                 GROUP BY ft.tag_id
               ) AS owners
         WHERE t.id = owners.tag_id
           AND t.user_id IS NULL;

        FOR rec IN
            SELECT DISTINCT
                   t.id AS source_tag_id,
                   mf.user_id AS owner_id,
                   t.name,
                   t.source,
                   t.normalized_name
              FROM file_tag ft
              JOIN media_file mf ON mf.id = ft.media_file_id
              JOIN tag t ON t.id = ft.tag_id
             WHERE mf.user_id IS NOT NULL
               AND t.user_id IS DISTINCT FROM mf.user_id
        LOOP
            SELECT id INTO copy_id
              FROM tag
             WHERE user_id = rec.owner_id AND name = rec.name
             LIMIT 1;

            IF copy_id IS NULL THEN
                INSERT INTO tag (uuid, name, source, normalized_name, user_id)
                VALUES (gen_random_uuid(), rec.name, rec.source,
                        rec.normalized_name, rec.owner_id)
                RETURNING id INTO copy_id;
            END IF;

            UPDATE file_tag ft
               SET tag_id = copy_id
              FROM media_file mf
             WHERE ft.media_file_id = mf.id
               AND ft.tag_id = rec.source_tag_id
               AND mf.user_id = rec.owner_id;
        END LOOP;
    END $$;
"""


def upgrade():
    # 1. Owner column. Nullable: NULL is a first-class value here (system tag).
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'tag' AND column_name = 'user_id'
            ) THEN
                ALTER TABLE tag ADD COLUMN user_id INTEGER REFERENCES "user"(id);
            END IF;
        END $$;
    """)

    # 2. Drop the global uniqueness on name *before* the backfill, which
    #    deliberately creates same-named rows for different owners. Any unique
    #    constraint or bare unique index whose key is exactly (name) is dropped,
    #    so DBs that acquired it under a non-default identifier are covered too.
    op.execute("""
        DO $$
        DECLARE
            con RECORD;
            idx RECORD;
        BEGIN
            FOR con IN
                SELECT conname FROM pg_constraint
                WHERE conrelid = 'tag'::regclass
                  AND contype = 'u'
                  AND conkey = ARRAY[(SELECT attnum FROM pg_attribute
                                      WHERE attrelid = 'tag'::regclass
                                        AND attname = 'name')]
            LOOP
                EXECUTE format('ALTER TABLE tag DROP CONSTRAINT %I', con.conname);
            END LOOP;

            FOR idx IN
                SELECT c.relname FROM pg_index i
                JOIN pg_class c ON c.oid = i.indexrelid
                WHERE i.indrelid = 'tag'::regclass
                  AND i.indisunique
                  AND i.indpred IS NULL
                  AND i.indnatts = 1
                  AND i.indkey[0] = (SELECT attnum FROM pg_attribute
                                     WHERE attrelid = 'tag'::regclass
                                       AND attname = 'name')
            LOOP
                EXECUTE format('DROP INDEX IF EXISTS %I', idx.relname);
            END LOOP;
        END $$;
    """)

    # 3. Backfill. Phase 1 claims each attached tag for its lowest-numbered
    #    owning user; phase 2 splits the mixed-ownership remainder.
    op.execute(BACKFILL_SQL)

    # 4. The replacement uniqueness. Created after the backfill so a
    #    half-migrated DB cannot deadlock the split against its own constraint.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_tag_user_name "
        "ON tag (user_id, name) WHERE user_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_tag_system_name ON tag (name) WHERE user_id IS NULL"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_tag_user_id ON tag (user_id)")


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_tag_user_id")
    op.execute("DROP INDEX IF EXISTS uq_tag_user_name")
    op.execute("DROP INDEX IF EXISTS uq_tag_system_name")
    op.execute("ALTER TABLE tag DROP COLUMN IF EXISTS user_id")

    # Restoring the global UNIQUE (name) is only possible when the split did not
    # produce duplicate names (single-user deployments). On a multi-user DB the
    # per-user copies are legitimate rows; merging them back would silently
    # re-share one user's tag with another, so the constraint is left off and
    # the duplicates are preserved. Deliberately a partial downgrade.
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM tag GROUP BY name HAVING COUNT(*) > 1) THEN
                ALTER TABLE tag ADD CONSTRAINT tag_name_key UNIQUE (name);
            END IF;
        END $$;
    """)
