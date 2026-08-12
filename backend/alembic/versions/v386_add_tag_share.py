"""v386: share a tag with specific users and groups.

Tags had exactly two visibility tiers: yours, or the whole deployment
(``user_id IS NULL``). There was no way to give one tag to a colleague or a
team, so a shared vocabulary meant publishing to everybody or duplicating the
word per person — the duplication this feature exists to stop.

``tag_share`` mirrors ``collection_share`` deliberately: same target shape
(exactly one of ``target_user_id`` / ``target_group_id``), same CASCADE
behaviour, same partial unique indexes. Sharing is already a solved problem in
this schema, and a second, differently-shaped grant table would be a second set
of rules to keep in step.

One deliberate difference: **no ``permission`` column.** A collection share
distinguishes viewer from editor because a collection carries files you might
be allowed to change. A tag share grants *vocabulary* — you can see the tag,
filter by it, and apply it — while renaming, merging and deleting stay with the
owner (or an admin, for system tags). Adding a column the authorization code
would always read as "viewer" would be a field that lies about being a choice.

**Raw idempotent SQL, not ``op.create_table``.** The startup runner
(``app/db/migrations.run_migrations``) stamps *untracked* databases by schema
fingerprint, so a revision routinely re-runs against a database that already
carries part of its changes — and a migration failure is ``SystemExit(1)``,
i.e. the backend refuses to start. ``op.create_table`` emits a bare
``CREATE TABLE``, which raises ``DuplicateTable`` in exactly that case; every
other revision in this chain is written with ``IF NOT EXISTS`` for this reason
(``backend/alembic/CLAUDE.md``). The emitted DDL is otherwise identical to what
``op.create_table`` produced: same auto-named foreign keys
(``tag_share_tag_id_fkey`` …), same ``tag_share_uuid_key`` unique constraint.

Revision ID: v386_add_tag_share
Revises: v385_drop_orphan_tables
"""

from alembic import op

revision = "v386_add_tag_share"
down_revision = "v385_drop_orphan_tables"
branch_labels = None
depends_on = None

#: Exactly one target, never both and never neither — the same guard
#: ``collection_share`` carries. Kept as one constant so the inline CHECK, the
#: repair guard below and the consistency test cannot drift apart.
TARGET_CHECK_SQL = (
    "(target_user_id IS NOT NULL AND target_group_id IS NULL) OR "
    "(target_user_id IS NULL AND target_group_id IS NOT NULL)"
)

#: Split into a template so the ``# nosec`` can sit on a code line (an f-string's
#: opening line has no room for a comment) — the shape v381/v382 already use.
_UPGRADE_TEMPLATE = """
    CREATE TABLE IF NOT EXISTS tag_share (
        id              SERIAL PRIMARY KEY,
        uuid            UUID NOT NULL DEFAULT gen_random_uuid() UNIQUE,
        tag_id          INTEGER NOT NULL REFERENCES tag(id) ON DELETE CASCADE,
        shared_by_id    INTEGER NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
        target_type     VARCHAR(20) NOT NULL,
        target_user_id  INTEGER REFERENCES "user"(id) ON DELETE CASCADE,
        target_group_id INTEGER REFERENCES user_group(id) ON DELETE CASCADE,
        created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT _tag_share_target_check CHECK ({target_check})
    );

    -- Repairs a table that exists WITHOUT the constraint (a run that died between
    -- statements, or a hand-built table). Without it, `CREATE TABLE IF NOT EXISTS`
    -- would silently accept a tag_share that admits both targets at once.
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = '_tag_share_target_check'
        ) THEN
            ALTER TABLE tag_share
                ADD CONSTRAINT _tag_share_target_check CHECK ({target_check});
        END IF;
    END $$;

    CREATE INDEX IF NOT EXISTS idx_tag_share_tag_id ON tag_share (tag_id);
    CREATE INDEX IF NOT EXISTS idx_tag_share_target_user_id ON tag_share (target_user_id);
    CREATE INDEX IF NOT EXISTS idx_tag_share_target_group_id ON tag_share (target_group_id);

    -- PARTIAL uniques, not a composite one: Postgres treats NULLs as distinct, so
    -- UNIQUE(tag_id, target_user_id, target_group_id) would happily admit the same
    -- grant twice. collection_share is indexed the same way for the same reason.
    CREATE UNIQUE INDEX IF NOT EXISTS _tag_share_user_uc
        ON tag_share (tag_id, target_user_id) WHERE target_user_id IS NOT NULL;
    CREATE UNIQUE INDEX IF NOT EXISTS _tag_share_group_uc
        ON tag_share (tag_id, target_group_id) WHERE target_group_id IS NOT NULL;
"""

#: Module-level so the consistency test can replay it and prove the re-run is a no-op,
#: rather than assert on this file's source text.
UPGRADE_SQL = _UPGRADE_TEMPLATE.format(target_check=TARGET_CHECK_SQL)  # nosec B608

#: Mirror image. ``DROP TABLE`` would take the indexes with it; they are dropped
#: explicitly so a database that somehow carries the indexes without the table
#: still ends up clean.
DOWNGRADE_SQL = """
    DROP INDEX IF EXISTS _tag_share_group_uc;
    DROP INDEX IF EXISTS _tag_share_user_uc;
    DROP INDEX IF EXISTS idx_tag_share_target_group_id;
    DROP INDEX IF EXISTS idx_tag_share_target_user_id;
    DROP INDEX IF EXISTS idx_tag_share_tag_id;
    DROP TABLE IF EXISTS tag_share;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
